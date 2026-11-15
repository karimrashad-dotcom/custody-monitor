"""استخراج بيانات الفاتورة من الصورة.

الترتيب:
1. كود QR الخاص بالفاتورة الإلكترونية (هيئة الزكاة والضريبة والجمارك): مجاني ويشتغل من غير نت.
   بيدي: اسم المورد، الرقم الضريبي، التاريخ، الإجمالي، الضريبة.
2. قراءة الصورة بـ Claude (لو فيه مفتاح API): بيكمّل رقم الفاتورة والأصناف والتصنيف.
3. لو الاتنين مش متاحين، الموظف يكتب البيانات بإيده.
"""
from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger("custody.extract")

DEFAULT_MODEL = "claude-haiku-5-5"
# موديلات بتدعم الرجوع التلقائي لموديل تاني لو الطلب اترفض
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-2"}


@dataclass
class Extraction:
    invoice_number: str = ""
    vendor_name: str = ""
    vendor_vat_number: str = ""
    invoice_date: str = ""
    subtotal: float | None = None
    vat_amount: float | None = None
    total: float | None = None
    category: str = ""
    items: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        return "+".join(self.sources) if self.sources else "manual"


# ---------------- QR (ZATCA TLV) ----------------
def parse_zatca_tlv(payload: str) -> dict | None:
    """يفك نص QR بتاع الفاتورة السعودية (TLV مشفر Base64)."""
    try:
        data = base64.b64decode(payload.strip() + "=" * (-len(payload.strip()) % 4))
    except Exception:
        return None
    fields: dict[int, str] = {}
    i = 0
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        value = data[i + 2 : i + 2 + length]
        if len(value) != length:
            break
        if tag in (1, 2, 3, 4, 5):
            try:
                fields[tag] = value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        i += 2 + length
    if not {1, 2, 4}.issubset(fields):
        return None
    return {
        "vendor_name": fields.get(1, ""),
        "vendor_vat_number": fields.get(2, ""),
        "timestamp": fields.get(3, ""),
        "total": fields.get(4, ""),
        "vat_amount": fields.get(5, ""),
    }


def decode_qr(image_bytes: bytes) -> str | None:
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover
        log.warning("OpenCV غير متاح، تخطي قراءة QR")
        return None
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    detector = cv2.QRCodeDetector()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    candidates = [img, gray]
    h, w = gray.shape[:2]
    if max(h, w) < 1600:
        candidates.append(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    candidates.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
    for candidate in candidates:
        try:
            text, _, _ = detector.detectAndDecode(candidate)
        except cv2.error:
            continue
        if text:
            return text
    return _decode_qr_zxing(gray) or _decode_qr_located(detector, gray) or _decode_qr_tiles(detector, gray)


def _decode_qr_located(detector, gray) -> str | None:
    """الكاشف ساعات بيلاقي مكان الكود من غير ما يقدر يفكه: نقصه ونكبّره ونوضحه ونجرب تاني."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    scale = min(1.0, 1600 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    quads = []
    try:
        ok, pts = detector.detectMulti(small)
        if ok and pts is not None:
            quads += list(pts)
    except cv2.error:
        pass
    try:
        ok, pts = detector.detect(small)
        if ok and pts is not None:
            quads.append(pts.reshape(4, 2))
    except cv2.error:
        pass
    for q in quads:
        q = np.asarray(q, dtype=np.float32).reshape(4, 2) / scale
        x0, y0 = q.min(0); x1, y1 = q.max(0)
        m = max(x1 - x0, y1 - y0) * 0.25
        x0, y0 = int(max(0, x0 - m)), int(max(0, y0 - m))
        x1, y1 = int(min(w, x1 + m)), int(min(h, y1 + m))
        crop = gray[y0:y1, x0:x1]
        if crop.size == 0:
            continue
        for f in (1, 2, 3):
            img = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC) if f > 1 else crop
            for v in (img, cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
                try:
                    text, _, _ = detector.detectAndDecode(v)
                except cv2.error:
                    continue
                if text:
                    return text
    return None


def _decode_qr_zxing(gray) -> str | None:
    """zxing-cpp (لو متثبتة) أقوى بكتير في لقط الكود من صورة كاميرا كاملة."""
    try:
        import zxingcpp
    except ImportError:
        return None
    try:
        for r in zxingcpp.read_barcodes(gray):
            if r.text and "QR" in str(r.format).upper():
                return r.text
    except Exception:
        log.exception("zxing")
    return None


def _decode_qr_tiles(detector, gray, budget: float = 6.0) -> str | None:
    """صورة كاميرا: الكود صغير جوه الصورة، فبندور عليه في مربعات صغيرة مكبّرة."""
    import time

    import cv2

    start = time.monotonic()
    h, w = gray.shape[:2]
    if max(h, w) > 2400:
        s = 2400 / max(h, w)
        gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        h, w = gray.shape[:2]
    for frac in (0.3, 0.22, 0.4):
        t = int(min(h, w) * frac)
        step = max(1, t // 3)
        for y in range(0, max(1, h - t + 1), step):
            for x in range(0, max(1, w - t + 1), step):
                if time.monotonic() - start > budget:
                    return None
                tile = gray[y:y + t, x:x + t]
                big = cv2.resize(tile, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                for img in (tile, big, cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
                    try:
                        text, _, _ = detector.detectAndDecode(img)
                    except cv2.error:
                        continue
                    if text:
                        return text
    return None


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return round(float(str(value).replace(",", "")), 2)
    except ValueError:
        return None


def from_qr(image_bytes: bytes, result: Extraction) -> None:
    text = decode_qr(image_bytes)
    if not text:
        return
    parsed = parse_zatca_tlv(text)
    if not parsed:
        result.notes.append("يوجد رمز QR لكنه ليس بصيغة الفاتورة الإلكترونية")
        return
    result.sources.append("qr")
    result.raw["qr"] = parsed
    result.vendor_name = parsed["vendor_name"]
    result.vendor_vat_number = parsed["vendor_vat_number"]
    result.total = _to_float(parsed["total"])
    result.vat_amount = _to_float(parsed["vat_amount"])
    if result.total is not None and result.vat_amount is not None:
        result.subtotal = round(result.total - result.vat_amount, 2)
    if parsed["timestamp"]:
        result.invoice_date = parsed["timestamp"][:10]


# ---------------- Claude ----------------
_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_invoice": {"type": "boolean"},
        "invoice_number": {"type": "string"},
        "vendor_name": {"type": "string"},
        "vendor_vat_number": {"type": "string"},
        "invoice_date": {"type": "string"},
        "subtotal": _NUM,
        "vat_amount": _NUM,
        "total": _NUM,
        "category": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": _NUM,
                    "unit_price": _NUM,
                    "line_total": _NUM,
                },
                "required": ["description", "quantity", "unit_price", "line_total"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": [
        "is_invoice", "invoice_number", "vendor_name", "vendor_vat_number",
        "invoice_date", "subtotal", "vat_amount", "total", "category", "items", "notes",
    ],
    "additionalProperties": False,
}

PROMPT = """This is a photo of a purchase or maintenance invoice from Saudi Arabia, taken on a phone by an employee who pays from a petty-cash custody.
Read it and return the fields in the schema.

- invoice_number: the invoice / receipt number exactly as printed (رقم الفاتورة). Empty string if none.
- vendor_name: the seller's name as printed, preferring the Arabic name if both exist.
- vendor_vat_number: the seller's 15-digit VAT registration number (الرقم الضريبي). Empty string if none.
- invoice_date: YYYY-MM-DD (convert Hijri to Gregorian if only Hijri is shown). Empty string if unreadable.
- subtotal: amount before VAT; vat_amount: VAT amount (usually 15%); total: final amount paid including VAT. Numbers in SAR, no currency symbols. null when not shown.
- items: each line item with description, quantity, unit price (before VAT if shown that way) and line total. Empty list if the lines are unreadable.
- category: choose exactly one of: {categories}
- is_invoice: false if the image is not an invoice or receipt.
- notes: one short Arabic sentence about anything doubtful (blurry, handwritten, totals don't add up). Empty string if fine.
Never guess numbers you cannot read: use null instead."""


def from_claude(image_bytes: bytes, media_type: str, api_key: str, model: str,
                categories: list[str], result: Extraction) -> None:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=120, max_retries=2)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    try:
        extra = {}
        if model in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        response = client.beta.messages.create(
            model=model,
            max_tokens=8000,
            **extra,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": INVOICE_SCHEMA},
            },
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": PROMPT.format(categories="، ".join(categories))},
                ],
            }],
        )
    except anthropic.AuthenticationError:
        result.notes.append("مفتاح Claude API غير صحيح، راجع الإعدادات")
        return
    except anthropic.RateLimitError:
        result.notes.append("خدمة القراءة الذكية مشغولة، أدخل البيانات يدوياً أو حاول مرة أخرى")
        return
    except anthropic.APIStatusError as exc:
        log.exception("Claude API error")
        result.notes.append(f"تعذرت القراءة الذكية (خطأ {exc.status_code})")
        return
    except anthropic.APIConnectionError:
        result.notes.append("لا يوجد اتصال بالإنترنت للقراءة الذكية")
        return

    if response.stop_reason == "refusal":
        result.notes.append("رفضت القراءة الذكية الصورة، أدخل البيانات يدوياً")
        return
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        result.notes.append("رد القراءة الذكية غير مكتمل")
        return

    result.sources.append("ai")
    result.raw["ai"] = data
    if not data.get("is_invoice", True):
        result.notes.append("هذه الصورة لا تبدو فاتورة")
    # QR أدق في الأرقام والمورد، فالذكاء الاصطناعي بيكمّل الناقص بس
    if not result.invoice_number:
        result.invoice_number = (data.get("invoice_number") or "").strip()
    if not result.vendor_name:
        result.vendor_name = (data.get("vendor_name") or "").strip()
    if not result.vendor_vat_number:
        result.vendor_vat_number = (data.get("vendor_vat_number") or "").strip()
    if not result.invoice_date:
        result.invoice_date = (data.get("invoice_date") or "").strip()
    for key in ("subtotal", "vat_amount", "total"):
        if getattr(result, key) is None:
            setattr(result, key, _to_float(data.get(key)))
    result.category = data.get("category") or ""
    result.items = [
        {
            "description": (it.get("description") or "").strip(),
            "quantity": _to_float(it.get("quantity")),
            "unit_price": _to_float(it.get("unit_price")),
            "line_total": _to_float(it.get("line_total")),
        }
        for it in data.get("items") or []
        if (it.get("description") or "").strip()
    ]
    if data.get("notes"):
        result.notes.append(data["notes"])


def from_pdf_text(text: str, result: Extraction, own: dict | None = None) -> None:
    from .ocr import parse_text

    data = parse_text(text, own)
    result.raw["pdf"] = {"fields": data, "text": text[:4000]}
    if not data:
        return
    result.sources.append("pdf")
    for key in ("invoice_number", "vendor_name", "vendor_vat_number", "invoice_date"):
        if not getattr(result, key) and data.get(key):
            setattr(result, key, data[key])
    for key in ("subtotal", "vat_amount", "total"):
        if getattr(result, key) is None and data.get(key) is not None:
            setattr(result, key, data[key])


# ---------------- قراءة مجانية (Tesseract) ----------------
def from_ocr(image_bytes: bytes, result: Extraction, own: dict | None = None,
             photo: bool = False) -> None:
    from . import ocr

    # صورة الكاميرا (خلفية، ضل، ميل): الأول نقص الورقة ونوحد الإضاءة، ولو منفعش نجرب الطريقة العادية
    text = ocr.read_text(image_bytes, photo=photo)
    data = ocr.parse_text(text, own) if text else {}
    if data.get("total") is None and result.total is None:
        text2 = ocr.read_text(image_bytes, photo=not photo)
        data2 = ocr.parse_text(text2, own) if text2 else {}
        if data2:
            data = {**data2, **{k: v for k, v in data.items() if v not in (None, "")}}
            text = (text or "") + "\n----\n" + text2
    if not text:
        return
    result.raw["ocr"] = {"fields": data, "text": text[:4000]}
    if not data:
        return
    result.sources.append("ocr")
    for key in ("invoice_number", "vendor_name", "vendor_vat_number", "invoice_date"):
        if not getattr(result, key) and data.get(key):
            setattr(result, key, data[key])
    for key in ("subtotal", "vat_amount", "total"):
        if getattr(result, key) is None and data.get(key) is not None:
            setattr(result, key, data[key])


def register_heif() -> bool:
    """صور الآيفون (HEIC): تشتغل لو مكتبة pillow-heif متثبتة."""
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        return True
    except Exception:
        return False


def prepare_image(raw: bytes, max_side: int = 2000) -> tuple[bytes, str]:
    """يعدل اتجاه الصورة ويصغرها ويحولها JPEG عشان المساحة والقراءة."""
    from PIL import Image, ImageOps

    register_heif()

    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue(), "image/jpeg"


def extract_invoice(image_bytes: bytes, media_type: str, api_key: str = "",
                    model: str = DEFAULT_MODEL, categories: list[str] | None = None,
                    original_bytes: bytes | None = None, pdf_text: str = "",
                    own_company: dict | None = None, photo: bool = False,
                    extra_qr: bytes | None = None) -> Extraction:
    result = Extraction()
    # QR بيتقري أحسن من الصورة الأصلية قبل التصغير
    from_qr(original_bytes or image_bytes, result)
    if not result.sources and extra_qr:
        from_qr(extra_qr, result)
    if not result.sources and original_bytes:
        from_qr(image_bytes, result)
    if pdf_text.strip():
        from_pdf_text(pdf_text, result, own_company)
    needs_ai = not result.invoice_number or result.total is None or not result.items
    if "pdf" in result.sources and result.total is not None and not api_key:
        needs_ai = False  # نص الـ PDF أدق من قراءة الصورة
    if api_key and needs_ai:
        from_claude(image_bytes, media_type, api_key, model or DEFAULT_MODEL,
                    categories or [], result)
    elif needs_ai:
        # القراءة المجانية أدق بكتير على الصورة بحجمها الكامل مش النسخة المتصغرة
        from_ocr(original_bytes or image_bytes, result, own_company, photo)
        if "ocr" not in result.sources and original_bytes:
            from_ocr(image_bytes, result, own_company, photo)
    if result.subtotal is None and result.total is not None and result.vat_amount is not None:
        result.subtotal = round(result.total - result.vat_amount, 2)
    if not result.sources and not api_key:
        result.notes.append("تعذّرت قراءة الفاتورة بوضوح، أدخل البيانات يدوياً")
    elif "ocr" in result.sources:
        result.notes.append("القراءة المجانية تقريبية، راجع الأرقام جيداً قبل الإرسال")
    return result
