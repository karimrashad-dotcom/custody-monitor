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


def _has(line: str, words) -> bool:
    low = line.lower()
    return any(w in low for w in words)


def parse_text(text: str) -> dict:
    text = normalize(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    result: dict = {}

    m = VAT_NO_RE.search(text.replace(" ", ""))
    if m:
        result["vendor_vat_number"] = m.group(0)

    result["invoice_date"] = find_date(text)

    m = INVOICE_NO_RE.search(text)
    if m and not VAT_NO_RE.fullmatch(m.group(1)):
        result["invoice_number"] = m.group(1)

    totals, vats, subs = [], [], []
    for ln in lines:
        nums = [n for n in numbers_in(ln) if n not in (15.0, 15)]
        if not nums:
            continue
        if _has(ln, SUBTOTAL_WORDS):
            subs.append(max(nums))
        elif _has(ln, VAT_WORDS) and not VAT_NO_RE.search(ln.replace(" ", "")):
            vats.extend(nums)
        elif _has(ln, TOTAL_WORDS):
            totals.append(max(nums))

    total = max(totals) if totals else None
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
    for ln in lines[:6]:
        letters = sum(ch.isalpha() for ch in ln)
        if letters >= 4 and not _has(ln, skip) and letters / max(len(ln), 1) > 0.5:
            result["vendor_name"] = ln[:80]
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
