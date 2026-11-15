"""قراءة مجانية لصورة الفاتورة على نفس الجهاز بـ Tesseract، من غير نت ومن غير API.

الدقة أقل من القراءة الذكية، خصوصاً في العربي وخط اليد، فبنعتمد على الأرقام والكلمات المفتاحية:
الرقم الضريبي السعودي (15 رقم يبدأ وينتهي بـ 3)، التاريخ، الإجمالي، الضريبة، رقم الفاتورة،
واسم المورد من أول سطور الفاتورة. الموظف بيراجع ويصحح قبل الإرسال.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import date
from pathlib import Path

log = logging.getLogger("custody.ocr")

WINDOWS_PATHS = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
]


def find_tesseract() -> str | None:
    custom = os.environ.get("TESSERACT_CMD")
    if custom and Path(custom).exists():
        return custom
    found = shutil.which("tesseract")
    if found:
        return found
    for p in WINDOWS_PATHS:
        if str(p) and p.exists():
            return str(p)
    return None


def status() -> dict:
    """للعرض في الإعدادات: هل Tesseract متثبت وفيه العربي؟"""
    cmd = find_tesseract()
    if not cmd:
        return {"installed": False, "arabic": False}
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        langs = pytesseract.get_languages(config="")
    except Exception:
        return {"installed": False, "arabic": False}
    return {"installed": True, "arabic": "ara" in langs, "path": cmd}


# ---------------- تنظيف النص ----------------
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")


def normalize(text: str) -> str:
    return text.translate(_DIGITS).replace("\u200f", "").replace("\u200e", "")


NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+)(?![\d])")


def numbers_in(line: str) -> list[float]:
    out = []
    for m in NUM_RE.finditer(line):
        raw = m.group(1).replace(",", "")
        if len(raw.replace(".", "")) > 9:  # أرقام طويلة زي الرقم الضريبي أو الجوال
            continue
        try:
            out.append(float(raw))
        except ValueError:
            pass
    return out


VAT_NO_RE = re.compile(r"(?<!\d)3\d{13}3(?!\d)")
TOTAL_WORDS = ["الإجمالي", "الاجمالي", "اجمالي", "إجمالي", "المجموع", "الصافي", "صافي", "المستحق",
               "المطلوب", "total", "grand", "net", "amount due", "balance due"]
VAT_WORDS = ["ضريبة", "الضريبة", "القيمة المضافة", "vat", "tax"]
SUBTOTAL_WORDS = ["قبل الضريبة", "بدون ضريبة", "غير شامل", "subtotal", "sub total", "sub-total",
                  "before vat", "excl", "taxable"]
# الإجمالي النهائي شامل الضريبة: بنشوفه قبل كلمات الضريبة عشان سطر (الإجمالي بعد الضريبة) ميتحسبش ضريبة
GRAND_WORDS = ["شامل الضريبة", "شامل ضريبة", "بعد الضريبة", "inclusive", "incl. vat", "incl vat",
               "after vat", "total after vat", "invoice total", "grand total", "الإجمالي شامل",
               "الاجمالي شامل", "المبلغ المستحق", "amount due"]
NOT_GRAND = ["غير شامل", "exclusive", "before", "قبل"]
INV_CODE_RE = re.compile(r"(?<![A-Za-z])(INV[-/ ]?\d{2,4}[-/]\d{2,8}|INV[-/]?\d{4,10})", re.IGNORECASE)
INVOICE_NO_RE = re.compile(
    r"(?:رقم\s*(?:ال)?فاتورة|فاتورة\s*رقم|invoice\s*(?:no|number|#)|inv\s*(?:no|#)|receipt\s*(?:no|#)|bill\s*no|رقم\s*الإيصال)"
    r"\s*[:#.\-]*\s*([A-Za-z0-9][A-Za-z0-9\-/]{0,24})",
    re.IGNORECASE,
)
DATE_PATTERNS = [
    (re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"), ("y", "m", "d")),
    (re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](20\d{2})"), ("d", "m", "y")),
]


def find_date(text: str) -> str:
    for pattern, order in DATE_PATTERNS:
        for m in pattern.finditer(text):
            parts = dict(zip(order, (int(g) for g in m.groups())))
            try:
                d = date(parts["y"], parts["m"], parts["d"])
            except ValueError:
                continue
            if 2015 <= d.year <= date.today().year + 1:
                return d.isoformat()
    return ""


# علامات برامج المسح (CamScanner وغيره): مش اسم مورد ولا جزء من الفاتورة
SCANNER_MARKS = ["camscanner", "cam scanner", "scanned with", "scanned by", "adobe scan",
                 "microsoft lens", "office lens", "genius scan", "tapscanner", "tap scanner",
                 "clearscan", "notebloc", "scanner pro", "tinyscanner", "tiny scanner"]


def is_scanner_mark(line: str) -> bool:
    low = " ".join((line or "").lower().split())
    return any(m in low for m in SCANNER_MARKS)


def strip_scanner_marks(text: str) -> str:
    return "\n".join(ln for ln in (text or "").splitlines() if not is_scanner_mark(ln))


def _has(line: str, words) -> bool:
    low = line.lower()
    return any(w in low for w in words)


GENERIC_NAME_WORDS = {"شركة", "شركه", "مؤسسة", "مؤسسه", "company", "co", "ltd", "limited", "est",
                      "المحدودة", "المحدوده", "التجارية", "التجاريه", "trading", "group", "مجموعة"}


def _name_words(name: str) -> set[str]:
    words = re.findall(r"[^\W\d_]{3,}", (name or "").casefold())
    return {w for w in words if w not in GENERIC_NAME_WORDS}


VENDOR_KEYWORDS = ["شركة", "شركه", "مؤسسة", "مؤسسه", "مطبعة", "مطبعه", "مصنع", "مكتب", "محل",
                   "معرض", "مركز", "ورشة", "ورشه", "صيدلية", "مطعم", "company", "co.", "est.",
                   "establishment", "trading", "llc", "ltd", "limited", "factory", "agency"]


def _vendor_keyword(line: str, extra: list[str] | None = None) -> bool:
    words = re.findall(r"[^\W\d_]+\.?", line.casefold())
    if any(w in VENDOR_KEYWORDS or w.rstrip(".") in VENDOR_KEYWORDS for w in words):
        return True
    low = line.casefold()
    return any(k in low for k in (extra or []))


def split_keywords(value: str) -> list[str]:
    """كلمات الإعدادات: مفصولة بفاصلة أو سطر جديد."""
    return [w.strip().casefold() for w in re.split(r"[,،\n]", value or "") if w.strip()]


def _is_own_line(line: str, own_words: set[str], loose: bool = False) -> bool:
    """السطر ده فيه اسم شركتنا (العميل في الفاتورة) مش اسم المورد.
    loose: كلمة واحدة مميزة من اسمنا كفاية (للسطور اللي فيها شركة/Company)."""
    if not own_words:
        return False
    found = own_words & _name_words(line)
    return len(found) >= (1 if loose else min(2, len(own_words)))


def parse_text(text: str, own: dict | None = None) -> dict:
    """own: بيانات شركتنا (name, vat) عشان منخدهاش على إنها المورد، لأن الفاتورة فيها بيانات العميل كمان."""
    own = own or {}
    kw = own.get("keywords") or {}
    total_words = TOTAL_WORDS + kw.get("total", [])
    grand_words = GRAND_WORDS + kw.get("total", [])
    vat_words = VAT_WORDS + kw.get("vat", [])
    sub_words = SUBTOTAL_WORDS + kw.get("subtotal", [])
    own_vat = re.sub(r"\D", "", own.get("vat") or "")
    own_words = _name_words(own.get("name") or "")
    text = strip_scanner_marks(normalize(text))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result: dict = {}

    # نشيل بلوك بيانات شركتنا (من سطر الاسم لحد سطر رقمها الضريبي) من دور اسم المورد
    name_lines = list(lines)
    own_idx = [i for i, ln in enumerate(lines) if _is_own_line(ln, own_words)]
    if own_idx and not own_vat:
        # رقمنا الضريبي مش متسجل: أول رقم ضريبي بعد اسم شركتنا على طول غالباً بتاعنا
        for ln in lines[own_idx[0]:own_idx[0] + 8]:
            m = VAT_NO_RE.search(ln.replace(" ", ""))
            if m:
                own_vat = m.group(0)
                break
    if own_idx:
        start = own_idx[0]
        end = start
        if own_vat:
            vat_idx = [i for i, ln in enumerate(lines) if own_vat in ln.replace(" ", "") and i >= start]
            if vat_idx and vat_idx[0] - start <= 10:
                end = vat_idx[0]
        name_lines = lines[:start] + lines[end + 1:]
        name_lines = [ln for ln in name_lines if not _is_own_line(ln, own_words)]

    def _is_own_vat(v: str) -> bool:  # رقمنا ممكن يتقري بكام رقم غلط
        return bool(own_vat) and len(v) == len(own_vat) and sum(a != b for a, b in zip(v, own_vat)) <= 4

    vats = [v for v in VAT_NO_RE.findall(text.replace(" ", "")) if not _is_own_vat(v)]
    if vats:
        result["vendor_vat_number"] = vats[0]

    result["invoice_date"] = find_date(text)

    m = None
    for word in kw.get("invoice_no", []):  # كلمات المستخدم الأول
        m = re.search(re.escape(word) + r"\s*[:#.\-]*\s*([A-Za-z0-9][A-Za-z0-9\-/]{0,24})", text, re.IGNORECASE)
        if m and any(ch.isdigit() for ch in m.group(1)):
            break
        m = None
    m = m or INVOICE_NO_RE.search(text)
    if m and not VAT_NO_RE.fullmatch(m.group(1)) and any(ch.isdigit() for ch in m.group(1)):
        result["invoice_number"] = m.group(1)
    else:
        m = INV_CODE_RE.search(text)
        if m:
            result["invoice_number"] = m.group(1).replace(" ", "")

    totals, vats, subs, grands = [], [], [], []
    for ln in lines:
        nums = [n for n in numbers_in(ln) if n not in (15.0, 15)]
        if not nums:
            continue
        if _has(ln, grand_words) and not _has(ln, NOT_GRAND):
            grands.append(max(nums))
        elif _has(ln, sub_words):
            subs.append(max(nums))
        elif _has(ln, vat_words) and not VAT_NO_RE.search(ln.replace(" ", "")):
            vats.extend(nums)
        elif _has(ln, total_words):
            totals.append(max(nums))

    total = max(grands) if grands else (max(totals) if totals else None)
    if total is None:
        decimals = [n for ln in lines for n in numbers_in(ln) if n != int(n)]
        total = max(decimals) if decimals else None
    if total is not None:
        result["total"] = round(total, 2)
        expected_vat = total * 15 / 115
        candidates = [v for v in vats if 0 < v < total]
        if candidates:
            best = min(candidates, key=lambda v: abs(v - expected_vat))
            if abs(best - expected_vat) <= max(1.0, expected_vat * 0.05):
                result["vat_amount"] = round(best, 2)
        if subs:
            sub = max(s for s in subs)
            if sub < total:
                result["subtotal"] = round(sub, 2)

    # اسم المورد: أول سطر فيه حروف كفاية ومش عنوان عام
    skip = ["فاتورة", "invoice", "tax", "ضريبية", "simplified", "مبسطة", "receipt", "إيصال"]
    def ok(ln: str) -> bool:
        letters = sum(ch.isalpha() for ch in ln)
        return letters >= 4 and not _has(ln, skip) and letters / max(len(ln), 1) > 0.5

    # الأول: سطر فيه كلمة زي (شركة، مؤسسة، Company) ومش اسم شركتنا. المورد بيبقى فوق والعميل تحت
    for ln in name_lines[:15]:
        if ok(ln) and _vendor_keyword(ln, kw.get("vendor")) and not _is_own_line(ln, own_words, loose=True):
            result["vendor_name"] = ln.strip(" /|-_:.,")[:80]
            return result
    for ln in name_lines[:6]:
        if ok(ln):
            result["vendor_name"] = ln.strip(" /|-_:.,")[:80]
            break
    return result


# ---------------- القراءة ----------------
def preprocess(image_bytes: bytes):
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) > 3200:  # صورة موبايل كبيرة جداً: نصغرها شوية عشان السرعة
        scale = 3200 / max(h, w)
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
    if max(h, w) < 1800:
        scale = 1800 / max(h, w)
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    img = cv2.fastNlMeansDenoising(img, h=10)
    img = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)
    return img


def read_text(image_bytes: bytes) -> str | None:
    cmd = find_tesseract()
    if not cmd:
        return None
    try:
        import pytesseract
    except ImportError:
        log.warning("pytesseract مش متثبت")
        return None
    pytesseract.pytesseract.tesseract_cmd = cmd
    img = preprocess(image_bytes)
    if img is None:
        return None
    try:
        langs = pytesseract.get_languages(config="")
        lang = "ara+eng" if "ara" in langs else "eng"
        return pytesseract.image_to_string(img, lang=lang, config="--psm 4", timeout=60)
    except Exception:
        log.exception("فشلت قراءة Tesseract")
        return None
