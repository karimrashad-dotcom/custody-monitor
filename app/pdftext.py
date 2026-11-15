"""ملفات PDF: الفواتير الإلكترونية وبيان العهدة المحفوظ PDF من Excel.

لو الـ PDF فيه نص (مش صورة ممسوحة)، بناخد الأرقام والكلام منه مباشرة، وده أدق بكتير من قراءة الصورة.
ملفات PDF كتير بتطلّع العربي مقلوب (آخر حرف الأول)، فبنعدله.
"""
from __future__ import annotations

import io
import re

ARABIC = re.compile(r"[؀-ۿ]")

# لام ألف بتطلع من الـ PDF حرف واحد فيه حرفين، فبنحميها قبل قلب الكلمة وبعدين نرجعها
LIGATURES = {"لا": "\uE000", "لإ": "\uE001", "لأ": "\uE002", "لآ": "\uE003"}
RESTORE = {v: k for k, v in LIGATURES.items()}


def _protect(page) -> None:
    for ch in page.chars:
        if ch.get("text") in LIGATURES:
            ch["text"] = LIGATURES[ch["text"]]


def _restore(text: str) -> str:
    for k, v in RESTORE.items():
        text = text.replace(k, v)
    return text
CID = re.compile(r"\(cid:\d+\)\s?")


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _clean(text: str) -> str:
    return CID.sub("", text or "").replace("ـ", "")


def _reverse_line(line: str) -> str:
    tokens = line.split(" ")
    return " ".join(t[::-1] if ARABIC.search(t) else t for t in reversed(tokens))


def looks_reversed(text: str) -> bool:
    """العربي الطبيعي كلماته كتير بتبدأ بـ (ال)، والمقلوب كلماته بتنتهي بـ (لا)."""
    words = [w for w in re.split(r"\s+", text) if ARABIC.search(w) and len(w) > 2]
    starts = sum(w.startswith("ال") for w in words)
    ends = sum(w.endswith("لا") for w in words)
    return ends > starts


def fix_rtl(text: str, reverse: bool | None = None) -> str:
    text = _clean(text)
    if reverse is None:
        reverse = looks_reversed(text)
    if not reverse:
        return text
    return "\n".join(_reverse_line(ln) if ARABIC.search(ln) else ln for ln in text.splitlines())


MIN_REAL_TEXT = 40  # أقل من كده يبقى الـ PDF صورة ممسوحة، والنص اللي فيه علامة مائية بس


def extract_text(data: bytes, max_pages: int = 3) -> str:
    """نص الـ PDF. لو ممسوح (CamScanner وغيره) بيرجع فاضي عشان الفاتورة تتقري من الصورة."""
    import pdfplumber

    from .ocr import strip_scanner_marks

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = pdf.pages[:max_pages]
        for p in pages:
            _protect(p)
        raw = "\n".join((p.extract_text() or "") for p in pages)
    text = strip_scanner_marks(_restore(fix_rtl(raw)))
    if sum(ch.isalnum() for ch in text) < MIN_REAL_TEXT:
        return ""
    return text


def extract_tables(data: bytes, max_pages: int = 5, by_text: bool = False) -> list[list]:
    """كل صفوف الجداول في الملف، بعد تعديل اتجاه العربي."""
    import pdfplumber

    rows: list[list] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in pdf.pages[:max_pages]:
            _protect(p)
        sample = "\n".join((p.extract_text() or "") for p in pdf.pages[:max_pages])
        reverse = looks_reversed(_clean(sample))
        for page in pdf.pages[:max_pages]:
            settings = {"vertical_strategy": "text", "horizontal_strategy": "text"} if by_text else {}
            for table in page.extract_tables(settings):
                for row in table:
                    cells = [_restore(fix_rtl(c, reverse)) if isinstance(c, str) else c for c in row]
                    # الجداول العربي بتطلع من اليمين للشمال مقلوبة، بنرجّعها لترتيب الأعمدة الطبيعي
                    rows.append(list(reversed(cells)) if reverse else cells)
    return rows


def render_first_page(data: bytes, scale: float = 4.0) -> bytes:
    """صورة JPEG لأول صفحة: للعرض ولقراءة QR."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[0]
        image = page.render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()
