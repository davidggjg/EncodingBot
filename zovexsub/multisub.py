"""כתוביות במכמה שפות לאותו סרטון — כדי לתרגם טוב יותר, בעיקר זכר ונקבה.

המשתמש שולח ZIP עם כתוביות מתוזמנות של אותו סרטון: אנגלית, צרפתית,
ספרדית, ערבית. אחת מהן היא המקור שמתורגם (התזמונים שלה נשמרים כמו
שהם), והשאר מוצמדות לכל שורה לפי זמן — כגרסאות נוספות של אותו משפט.

למה זה עוזר: באנגלית "you are ready" אינו אומר אם מדברים אל גבר או אל
אישה. בצרפתית "tu es prête", בספרדית "estás lista" ובערבית "أنتِ" —
כן. מתרגם שרואה את כולן יודע. זה תרגום רב-מקורי (multi-source): כל שפה
מבהירה את מה שהאחרות משאירות פתוח.
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import srt

log = logging.getLogger(__name__)

SUBS = (".srt", ".vtt", ".ass", ".ssa")

# קודי שפה בשם הקובץ: Movie.en.srt, Movie.French.srt, Movie_ara.srt
_TAGS = {
    "en": ("en", "eng", "english"), "fr": ("fr", "fre", "fra", "french"),
    "es": ("es", "spa", "spanish", "esp", "latino"),
    "ar": ("ar", "ara", "arabic"), "he": ("he", "heb", "hebrew", "iw"),
    "tr": ("tr", "tur", "turkish"), "de": ("de", "ger", "deu", "german"),
    "it": ("it", "ita", "italian"), "pt": ("pt", "por", "portuguese", "br"),
    "ru": ("ru", "rus", "russian"),
}
# מילים נפוצות, לשפה שלא סומנה בשם הקובץ
_COMMON = {
    "en": {"the", "you", "and", "is", "what", "this", "that", "it", "to"},
    "fr": {"le", "la", "les", "vous", "est", "pas", "je", "tu", "et", "que"},
    "es": {"el", "los", "que", "es", "no", "por", "qué", "estás", "usted", "y"},
    "tr": {"ve", "bir", "bu", "ne", "değil", "mı", "ben", "sen", "için"},
    "de": {"der", "die", "und", "ist", "nicht", "ich", "du", "das", "sie"},
    "it": {"il", "che", "non", "sono", "di", "è", "per", "una", "lei"},
    "pt": {"o", "que", "não", "é", "você", "um", "uma", "está", "de"},
}
NAMES = {"en": "אנגלית", "fr": "צרפתית", "es": "ספרדית", "ar": "ערבית",
         "he": "עברית", "tr": "טורקית", "de": "גרמנית", "it": "איטלקית",
         "pt": "פורטוגזית", "ru": "רוסית"}
# סדר העדפה למקור שמתורגם: אנגלית קודם, כי התרגום הכי מכויל אליה
PRIMARY_ORDER = ("en", "fr", "es", "tr", "de", "it", "pt", "ru", "ar")


def is_zip(path: Path) -> bool:
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def refs_dir(srt_path: Path) -> Path:
    return srt_path.with_name(srt_path.name + ".refs")


def language(path: Path, text: str) -> str:
    """שפת הקובץ: קודם לפי השם, אחר כך לפי הכתב והמילים."""
    tokens = re.split(r"[^a-z]+", path.stem.lower())
    for code, tags in _TAGS.items():
        if any(t in tags for t in tokens):
            return code
    if re.search(r"[؀-ۿ]", text):
        return "ar"
    if re.search(r"[א-ת]", text):
        return "he"
    if re.search(r"[Ѐ-ӿ]", text):
        return "ru"
    words = re.findall(r"[a-zà-ÿğışçöü]+", text.lower())
    scores = {code: sum(w in common for w in words)
              for code, common in _COMMON.items()}
    best = max(scores, key=scores.get) if scores else ""
    return best if best and scores[best] > 5 else "xx"


def unpack(bundle: Path, target: Path) -> dict[str, int]:
    """פורק את ה-ZIP: המקור ל-target, והשאר לתיקיית הגרסאות לצדו.

    מחזיר שפה ← מספר כתוביות, והמקור מסומן במפתח "*"
    """
    found: list[tuple[str, Path, int]] = []
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(bundle) as archive:
            for member in archive.infolist():
                name = Path(member.filename).name
                if member.is_dir() or not name.lower().endswith(SUBS):
                    continue
                # שם בטוח: בלי תיקיות מהארכיון (‎../‎) — רק שם הקובץ
                out = Path(tmp) / f"{len(found):02d}_{name}"
                with archive.open(member) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                text = srt.read_subtitle(out)
                cues = srt.parse(text)
                if cues:
                    found.append((language(out, text), out, len(cues)))
        if not found:
            raise ValueError("ב-ZIP אין קובצי כתוביות שאפשר לקרוא")
        # מקור: לפי סדר ההעדפה, ובתוך אותה שפה — הקובץ עם הכי הרבה כתוביות
        rank = {code: n for n, code in enumerate(PRIMARY_ORDER)}
        found.sort(key=lambda f: (rank.get(f[0], 99), -f[2]))
        primary = found[0]
        srt.normalize(primary[1], target)
        folder = refs_dir(target)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        summary = {"*": primary[0]}
        used: set[str] = set()
        for code, path, count in found[1:]:
            if code in used or code == primary[0]:
                continue           # שפה אחת לגרסה — הראשונה (הכי מלאה)
            used.add(code)
            srt.normalize(path, folder / f"{code}.srt")
            summary[code] = count
        summary[primary[0]] = primary[2]
    log.info("ZIP כתוביות: מקור %s, גרסאות %s", primary[0],
             ", ".join(c for c in summary if c not in ("*", primary[0])) or "אין")
    return summary


def move(old_srt: Path, new_srt: Path) -> None:
    """מעביר את תיקיית הגרסאות יחד עם קובץ המקור."""
    old = refs_dir(old_srt)
    if old.is_dir():
        new = refs_dir(new_srt)
        shutil.rmtree(new, ignore_errors=True)
        shutil.move(str(old), str(new))


def attach(segments: list, srt_path: Path) -> int:
    """מצמיד לכל שורה את הגרסאות שלה בשפות האחרות, לפי חפיפה בזמן."""
    folder = refs_dir(srt_path)
    if not folder.is_dir():
        return 0
    attached = 0
    for ref in sorted(folder.glob("*.srt")):
        cues = srt.parse(srt.read_subtitle(ref))
        if not cues:
            continue
        for seg in segments:
            span = max(0.2, seg.end - seg.start)
            texts = []
            for cue in cues:
                if cue.end <= seg.start or cue.start >= seg.end:
                    continue
                overlap = min(cue.end, seg.end) - max(cue.start, seg.start)
                if overlap >= 0.4 * min(span, max(0.2, cue.end - cue.start)):
                    texts.append(" ".join((cue.text or "").split()))
            if texts:
                seg.refs[ref.stem] = " / ".join(texts)[:240]
                attached += 1
    log.info("גרסאות בשפות אחרות הוצמדו: %d צמדים", attached)
    return attached
