"""בדיקה אחרונה לפני כל קובץ: שורות שהעברית בהן נשברה — מתקן Groq.

למה Groq ולא ג'ימיני: האותיות הזרות ששרדו לקבצים הגיעו בדיוק כשהמכסה
של ג'ימיני נגמרה בסוף העבודה — שלב התיקון לא מצא מפתח פנוי ונכשל תוך
שנייה (נמדד אצל המשתמש). Groq הוא שירות נפרד עם מכסה משלו, ולכן הוא
מתקן גם כשג'ימיני תקוע.

הבדיקה עצמה אינה מודל: cleanup.clean מזהה כל שורה עם אותיות ערביות,
סיניות, יפניות, קיריליות וכו' — ורק השורות האלה נשלחות. תיקון מתקבל רק
אם הוא עברית נקייה; אחרת השורה נשארת כמו שהייתה.
"""
from __future__ import annotations

import json
import logging

from . import cleanup, config

log = logging.getLogger(__name__)

URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM = """אתה מגיה כתוביות בעברית. בכל שורה שאתה מקבל השתרבבו מילים או
אותיות משפה אחרת — ערבית, סינית, יפנית, רוסית או אנגלית. לכל שורה מצורף
גם המשפט המקורי שממנו היא תורגמה.

החזר את השורה בעברית תקנית וטבעית בלבד, באותה משמעות ובאותו אורך בערך.
אל תוסיף הסברים. שמות של אנשים ומקומות כתוב באותיות עבריות.
החזר JSON בלבד: {"lines": [{"i": <מספר>, "he": "<השורה המתוקנת>"}]}"""


def available() -> bool:
    return bool(config.GROQ_API_KEYS) and config.GROQ_FIX


def _accept(text: str) -> bool:
    """תיקון מתקבל רק כשהוא עברית נקייה."""
    text = (text or "").strip()
    return (bool(text) and bool(cleanup.HEBREW.search(text))
            and not cleanup.has_foreign(text))


async def fix(items: list[tuple[int, str, str]]) -> dict[int, str]:
    """items: (אינדקס, מקור, שורה שבורה). מחזיר אינדקס ← שורה מתוקנת."""
    if not items or not available():
        return {}
    import httpx
    listing = "\n".join(f"[{i}] מקור: {src}\n[{i}] שבורה: {bad}"
                        for i, src, bad in items)
    fixed: dict[int, str] = {}
    last = ""
    for model in config.GROQ_FIX_MODELS:
        for key in config.GROQ_API_KEYS:
            try:
                async with httpx.AsyncClient(timeout=90) as client:
                    answer = await client.post(
                        URL, headers={"Authorization": f"Bearer {key}"},
                        json={"model": model, "temperature": 0.2,
                              "response_format": {"type": "json_object"},
                              "messages": [
                                  {"role": "system", "content": SYSTEM},
                                  {"role": "user", "content": listing}]})
                if answer.status_code in (429, 503):
                    last = f"{model}: {answer.status_code}"
                    continue
                answer.raise_for_status()
                body = answer.json()["choices"][0]["message"]["content"]
                for row in json.loads(body).get("lines", []):
                    index, text = int(row.get("i", -1)), str(row.get("he", ""))
                    if any(index == i for i, _, _ in items) and _accept(text):
                        fixed[index] = text.strip()
                log.info("Groq (%s) תיקן %d מתוך %d שורות שבורות",
                         model, len(fixed), len(items))
                return fixed
            except Exception as exc:  # noqa: BLE001 — מנסים מפתח/מודל אחר
                last = f"{model}: {str(exc)[:100]}"
                continue
    log.warning("תיקון Groq נכשל בכל המפתחות והמודלים (%s)", last)
    return fixed


PROOF_SYSTEM = """אתה מגיה כתוביות בעברית. תקבל שורות ממוספרות, כל אחת עם
המשפט המקורי שממנו תורגמה. מצא רק שורות עם שגיאה ברורה:
- מילה שבורה או שגיאת כתיב ("מטאטן" במקום "מטאטא", "שתגעת" במקום "השתגעת")
- אותיות או מילים משפה אחרת בתוך השורה (ערבית, סינית, רוסית וכו')
- מילה חסרת משמעות או משובשת

אל תשנה ניסוח שהוא תקין, גם אם היית מתרגם אחרת. אל תשנה סגנון, סלנג או
שמות. שורה תקינה — אל תחזיר אותה בכלל.
החזר JSON בלבד: {"lines": [{"i": <מספר>, "he": "<השורה המתוקנת>"}]}
אם אין שגיאות: {"lines": []}"""


def _close(old: str, new: str) -> bool:
    """תיקון נקודתי ולא ניסוח מחדש: רוב השורה נשארת כמו שהייתה."""
    import difflib
    if not new.strip():
        return False
    ratio = difflib.SequenceMatcher(None, old, new).ratio()
    return ratio >= config.GROQ_PROOF_MIN_SIMILARITY or cleanup.has_foreign(old)


async def _ask(model: str, key: str, system: str, listing: str) -> list[dict]:
    import httpx
    async with httpx.AsyncClient(timeout=90) as client:
        answer = await client.post(
            URL, headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "temperature": 0.1,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": listing}]})
    if answer.status_code in (429, 503):
        raise RuntimeError(f"{model}: {answer.status_code}")
    answer.raise_for_status()
    body = answer.json()["choices"][0]["message"]["content"]
    return json.loads(body).get("lines", [])


async def proofread(lines: list[str], sources: list[str]) -> dict[int, str]:
    """Groq עובר על כל השורות במנות, ומחזיר רק תיקונים נקודתיים בטוחים."""
    if not lines or not available() or not config.GROQ_PROOFREAD:
        return {}
    import asyncio
    size = max(20, config.GROQ_PROOF_BATCH)
    batches = [list(range(i, min(i + size, len(lines))))
               for i in range(0, len(lines), size)]
    gate = asyncio.Semaphore(max(1, len(config.GROQ_API_KEYS)) * 2)
    fixed: dict[int, str] = {}

    async def one(number: int, indices: list[int]) -> None:
        listing = "\n".join(f"[{i}] מקור: {sources[i]}\n[{i}] עברית: {lines[i]}"
                            for i in indices)
        keys = config.GROQ_API_KEYS
        async with gate:
            for attempt in range(len(config.GROQ_FIX_MODELS) * len(keys)):
                model = config.GROQ_FIX_MODELS[attempt // len(keys)]
                key = keys[(number + attempt) % len(keys)]
                try:
                    rows = await _ask(model, key, PROOF_SYSTEM, listing)
                except Exception as exc:  # noqa: BLE001
                    log.debug("הגהת Groq: %s", str(exc)[:100])
                    await asyncio.sleep(2)
                    continue
                for row in rows:
                    index, text = int(row.get("i", -1)), str(row.get("he", "")).strip()
                    if (index in indices and text != lines[index]
                            and _accept(text) and _close(lines[index], text)):
                        fixed[index] = text
                return

    await asyncio.gather(*(one(n, b) for n, b in enumerate(batches)))
    if fixed:
        sample = " · ".join(f"{lines[i][:25]} ← {fixed[i][:25]}"
                            for i in list(fixed)[:6])
        log.info("הגהת Groq: %d תיקונים ב-%d שורות (%s)", len(fixed),
                 len(lines), sample)
    else:
        log.info("הגהת Groq: %d שורות, אין תיקונים", len(lines))
    return fixed
