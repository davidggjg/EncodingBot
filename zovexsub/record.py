"""הורדת שידור חי בתזמון — רק למנהל.

המנהל שולח: ‎/record <קישור> <תאריך> <שעה> <דקות>‎. בשעה שנקבעה הבוט
מוריד את השידור — לא מקליט מסך: מוריד את חתיכות ה-HLS עצמן כמו שהשרת
משדר אותן, בלי קידוד, ולכן האיכות והגודל זהים למקור. אחרי מספר הדקות
שנקבע ההורדה נעצרת, הקרדיט נוסף לפתיחה בלי צריבה (media.replace_audio),
והקובץ עולה לטלגרם, עם פוסטר אם נשלח.

התזמון נשמר בקובץ ולא בזיכרון: בין התזמון לשידור יכולים לעבור ימים,
ובהם עדכונים והפעלות מחדש.

בשידורי כאן השמע הוא רצועה נפרדת. וידאו ושמע יורדים במקביל בשני
תהליכים, וכל אחד שומר את חותמות הזמן של השידור עצמו (MPEG-TS) — ולכן
החיבור ביניהם מסונכרן לפי שעון השידור, גם אם אחד התחיל שנייה אחרי השני.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

ZONE = ZoneInfo("Asia/Jerusalem")
_cover_wait: dict[int, str] = {}     # צ'אט ← מזהה התזמון שממתין לפוסטר


def _store() -> Path:
    return Path(config.RECORD_STORE)


def load() -> list[dict]:
    try:
        return json.loads(_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save(items: list[dict]) -> None:
    _store().parent.mkdir(parents=True, exist_ok=True)
    tmp = _store().with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(_store())


def parse_when(date: str, clock: str, now: datetime | None = None) -> datetime:
    """תאריך ושעה בשעון ישראל. תאריך: היום, מחר, 3/12, 03/12/2026, 2026-12-03."""
    now = now or datetime.now(ZONE)
    date = date.strip().lower()
    if date in ("היום", "today"):
        day = now.date()
    elif date in ("מחר", "tomorrow"):
        day = (now + timedelta(days=1)).date()
    elif re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", date):
        y, m, d = map(int, date.split("-"))
        day = datetime(y, m, d).date()
    else:
        found = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", date)
        if not found:
            raise ValueError(f"לא הבנתי את התאריך: {date}")
        d, m = int(found.group(1)), int(found.group(2))
        y = int(found.group(3)) if found.group(3) else now.year
        if y < 100:
            y += 2000
        day = datetime(y, m, d).date()
        # 3/1 בדצמבר — כנראה ינואר של השנה הבאה
        if not found.group(3) and day < now.date():
            day = datetime(y + 1, m, d).date()
    found = re.fullmatch(r"(\d{1,2}):(\d{2})", clock.strip())
    if not found:
        raise ValueError(f"לא הבנתי את השעה: {clock}")
    return datetime(day.year, day.month, day.day, int(found.group(1)),
                    int(found.group(2)), tzinfo=ZONE)


def add(chat: int, url: str, when: datetime, minutes: int) -> dict:
    item = {"id": uuid.uuid4().hex[:6], "chat": int(chat), "url": url,
            "start": when.timestamp(), "minutes": int(minutes),
            "cover": "", "status": "מתוזמן"}
    items = load()
    items.append(item)
    save(items)
    return item


def remove(item_id: str) -> bool:
    items = load()
    kept = [i for i in items if i["id"] != item_id]
    save(kept)
    return len(kept) != len(items)


def describe(item: dict) -> str:
    when = datetime.fromtimestamp(item["start"], ZONE)
    return (f"<code>{item['id']}</code> · {when:%d/%m %H:%M} · "
            f"{item['minutes']} דק׳ · {item['status']}"
            + (" · 🖼️" if item.get("cover") else ""))


# ── פוסטר ──────────────────────────────────────────────────────
def wanting_cover(chat) -> bool:
    return int(chat) in _cover_wait


def set_cover(chat, path: Path) -> bool:
    item_id = _cover_wait.pop(int(chat), "")
    items = load()
    for item in items:
        if item["id"] == item_id:
            item["cover"] = str(path)
            save(items)
            return True
    return False


def ask_cover(chat, item_id: str) -> None:
    _cover_wait[int(chat)] = item_id


# ── ההורדה עצמה ────────────────────────────────────────────────
async def _formats(url: str) -> tuple[str, str]:
    """(תבנית וידאו, תבנית שמע). שמע ריק = הווידאו כבר כולל שמע."""
    from . import fetch
    cmd = (fetch._command() + fetch._browser_args(url, _mode(url))
           + ["--no-config", "-4", "-J", "--no-playlist", url])
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise RuntimeError("קריאת השידור נכשלה: " + " | ".join(tail))
    info = json.loads(out)
    formats = info.get("formats") or []
    video = [f for f in formats if f.get("vcodec") not in (None, "none")
             and (f.get("height") or 0) <= config.RECORD_MAX_HEIGHT]
    if not video:
        video = [f for f in formats if f.get("vcodec") not in (None, "none")]
    if not video:
        return "best", ""
    best = max(video, key=lambda f: ((f.get("height") or 0),
                                     (f.get("tbr") or 0)))
    if best.get("acodec") not in (None, "none"):
        return best["format_id"], ""
    audio = [f for f in formats if f.get("vcodec") in (None, "none")
             and f.get("acodec") not in (None, "none")]
    if not audio:
        return best["format_id"], ""
    # שמע עברי קודם, אחרת הטוב ביותר
    audio.sort(key=lambda f: ((f.get("language") or "").startswith(("he", "iw")),
                              f.get("tbr") or 0), reverse=True)
    return best["format_id"], audio[0]["format_id"]


def _mode(url: str) -> str:
    return "kan" if "kan" in url.lower() or "redge" in url.lower() else "origin"


async def _grab(url: str, fmt: str, out: Path, seconds: float) -> None:
    """מוריד תבנית אחת מהשידור למשך seconds, ואז עוצר."""
    from . import fetch
    cmd = (fetch._command() + fetch._browser_args(url, _mode(url))
           + ["--no-config", "-4", "--no-part", "--hls-use-mpegts",
              "--retries", "infinite", "--fragment-retries", "infinite",
              "-f", fmt, "-o", str(out), url])
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        await asyncio.wait_for(proc.wait(), timeout=seconds)
        log.warning("ההורדה של %s נעצרה לפני הזמן (קוד %s)", fmt, proc.returncode)
    except asyncio.TimeoutError:
        # SIGINT ולא SIGKILL: yt-dlp סוגר את הקובץ בצורה מסודרת
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
        except asyncio.TimeoutError:
            fetch.kill_tree(proc)
    finally:
        fetch.kill_tree(proc)


def _found(base: Path) -> Path | None:
    """yt-dlp מוסיף סיומת משלו — מוצאים את הקובץ שנכתב בפועל."""
    hits = sorted(base.parent.glob(base.name + "*"),
                  key=lambda p: p.stat().st_size, reverse=True)
    return hits[0] if hits and hits[0].stat().st_size > 0 else None


async def record(item: dict, work: Path, on_progress=None) -> Path:
    """מוריד את השידור לפי התזמון ומחזיר MP4 עם קרדיט."""
    from . import media
    url, seconds = item["url"], item["minutes"] * 60
    work.mkdir(parents=True, exist_ok=True)
    video_fmt, audio_fmt = await _formats(url)
    log.info("הורדת שידור %s: וידאו %s, שמע %s, %d דקות", item["id"],
             video_fmt, audio_fmt or "(בתוך הווידאו)", item["minutes"])
    began = time.monotonic()

    async def ticker() -> None:
        while True:
            await asyncio.sleep(30)
            if on_progress:
                done = int(time.monotonic() - began)
                size = sum(p.stat().st_size for p in work.glob("rec-*"))
                await on_progress(
                    f"🔴 מוריד שידור · {done // 60}/{item['minutes']} דק׳ · "
                    f"{size / 1024 ** 3:.2f}GB")

    pulse = asyncio.ensure_future(ticker())
    try:
        jobs = [_grab(url, video_fmt, work / "rec-v", seconds)]
        if audio_fmt:
            jobs.append(_grab(url, audio_fmt, work / "rec-a", seconds))
        await asyncio.gather(*jobs)
    finally:
        pulse.cancel()

    video = _found(work / "rec-v")
    if video is None:
        raise RuntimeError("השידור לא ירד — לא נכתב שום קובץ")
    merged = work / "merged.mkv"
    if audio_fmt:
        audio = _found(work / "rec-a")
        if audio is None:
            raise RuntimeError("השמע של השידור לא ירד")
        if on_progress:
            await on_progress("🎵 מחבר וידאו ושמע לפי שעון השידור…")
        # ‎-copyts‎: שני הקבצים נושאים את חותמות הזמן של השידור עצמו,
        # ולכן הם מסונכרנים גם אם תהליך אחד התחיל שנייה אחרי השני
        await media._run(["ffmpeg", "-nostdin", "-y", "-copyts",
                          "-i", str(video), "-i", str(audio),
                          "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                          "-start_at_zero", str(merged)])
    else:
        await media._run(["ffmpeg", "-nostdin", "-y", "-i", str(video),
                          "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                          str(merged)])
    for part in work.glob("rec-*"):
        part.unlink(missing_ok=True)

    # הקרדיט: אותו מנגנון של החלפת שמע — רק הפתיחה מקודדת, השאר מועתק.
    # השמע נלקח מאותו קובץ עצמו, ולכן נשאר מסונכרן
    out = work / f"live-{item['id']}.mp4"
    sound = merged if await media.has_audio(merged) else None
    await media.replace_audio(merged, sound, out, on_progress=on_progress)
    merged.unlink(missing_ok=True)
    log.info("שידור %s ירד: %.2fGB ב-%.0f דקות", item["id"],
             out.stat().st_size / 1024 ** 3, (time.monotonic() - began) / 60)
    return out


# ── מתזמן ──────────────────────────────────────────────────────
_running: set[str] = set()


async def run_forever(deliver) -> None:
    """בודק כל חצי דקה אם הגיע זמן של שידור, ומפעיל אותו.

    deliver(item, path) מעלה את התוצאה. תזמון שעבר בזמן שהבוט היה כבוי
    מתחיל מיד אם עוד לא עבר כל החלון שלו — עדיף חלק מהשידור משום כלום
    """
    while True:
        try:
            now = time.time()
            for item in load():
                if item["status"] != "מתוזמן" or item["id"] in _running:
                    continue
                end = item["start"] + item["minutes"] * 60
                if now >= end:
                    _mark(item["id"], "פוספס — הבוט היה כבוי")
                    continue
                if now >= item["start"] - 5:
                    left = (end - now) / 60
                    if left < item["minutes"] - 1:
                        log.warning("שידור %s מתחיל באיחור — נשארו %.0f דקות",
                                    item["id"], left)
                        item = {**item, "minutes": max(1, int(left))}
                    _running.add(item["id"])
                    asyncio.ensure_future(_one(item, deliver))
        except Exception:  # noqa: BLE001 — המתזמן לא מת
            log.exception("מתזמן השידורים נכשל בסבב")
        await asyncio.sleep(30)


def _mark(item_id: str, status: str) -> None:
    items = load()
    for item in items:
        if item["id"] == item_id:
            item["status"] = status
    save(items)


async def _one(item: dict, deliver) -> None:
    _mark(item["id"], "מוריד")
    try:
        await deliver(item)
        _mark(item["id"], "הסתיים")
    except Exception as exc:  # noqa: BLE001
        log.exception("הורדת שידור %s נכשלה", item["id"])
        _mark(item["id"], f"נכשל: {str(exc)[:80]}")
    finally:
        _running.discard(item["id"])
