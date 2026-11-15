"""القراءة المجانية بـ Tesseract على صورة فاتورة متولدة شبه صورة الموبايل."""
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app import extract, ocr

from pathlib import Path

from PIL import features

FONT = next((str(p) for p in [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                                Path(r"C:\Windows\Fonts\arial.ttf")] if p.exists()), None)
CAN_DRAW_ARABIC = bool(FONT) and features.check("raqm")


def make_invoice(lines, rotate=1.5, blur=0.8) -> bytes:
    img = Image.new("RGB", (900, 120 + 60 * len(lines)), (250, 248, 240))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 30)
    y = 50
    for text in lines:
        d.text((850, y), text, font=font, fill=(20, 20, 20), anchor="ra", direction="rtl" if any("؀" <= c <= "ۿ" for c in text) else None)
        y += 60
    img = img.rotate(rotate, expand=True, fillcolor=(200, 200, 200)).filter(ImageFilter.GaussianBlur(blur))
    arr = np.array(img).astype(np.int16) + np.random.default_rng(1).integers(-12, 12, (img.height, img.width, 3))
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def test_parse_text_rules():
    text = """مؤسسة الريان للتجارة
فاتورة ضريبية مبسطة
رقم الفاتورة: INV-20451
التاريخ: ١٢/١١/٢٠٢٦
الرقم الضريبي: 310122393500003
الإجمالي قبل الضريبة 400.00
ضريبة القيمة المضافة 15% 60.00
الإجمالي 460.00"""
    r = ocr.parse_text(text)
    assert r["vendor_name"] == "مؤسسة الريان للتجارة"
    assert r["invoice_number"] == "INV-20451"
    assert r["invoice_date"] == "2026-11-12"
    assert r["vendor_vat_number"] == "310122393500003"
    assert r["total"] == 460.0 and r["vat_amount"] == 60.0 and r["subtotal"] == 400.0


@pytest.mark.skipif(not ocr.find_tesseract() or not CAN_DRAW_ARABIC,
                    reason="Tesseract أو رسم العربي (libraqm) مش متاح")
def test_ocr_on_photo():
    img = make_invoice([
        "مؤسسة الريان للتجارة",
        "فاتورة ضريبية مبسطة",
        "Invoice No: 20451",
        "Date: 2026-11-12",
        "VAT No: 310122393500003",
        "زيت محرك 4 لتر    120.00",
        "فلتر زيت          80.00",
        "Subtotal 400.00",
        "VAT 15% 60.00",
        "Total 460.00",
    ])
    prepared, mt = extract.prepare_image(img)
    res = extract.extract_invoice(prepared, mt, original_bytes=img)
    print(res.raw.get("ocr", {}).get("text"))
    assert "ocr" in res.sources
    assert res.total == 460.0
    assert res.vat_amount == 60.0
    assert res.vendor_vat_number == "310122393500003"
    assert res.invoice_date == "2026-11-12"
    assert res.invoice_number == "20451"


@pytest.mark.skipif(not ocr.find_tesseract() or not CAN_DRAW_ARABIC,
                    reason="Tesseract أو رسم العربي (libraqm) مش متاح")
def test_ocr_on_camera_photo():
    """صورة كاميرا: الفاتورة صغيرة في نص الصورة، مايلة، وعليها ضل."""
    import cv2
    page = np.array(Image.open(io.BytesIO(make_invoice([
        "شركة الأفق للتجارة المحدودة", "Invoice No: 88213", "Date: 2026-11-10",
        "VAT No: 310455667700003", "Subtotal 300.00", "VAT 15% 45.00", "Total 345.00"], rotate=0, blur=0.3))))
    h, w = page.shape[:2]
    W, H = 4000, 3000
    bg = np.clip(np.full((H, W, 3), (70, 90, 110)) + np.random.default_rng(2).integers(-20, 20, (H, W, 3)), 0, 255).astype(np.uint8)
    ph = H * 0.45; pw = ph * w / h; cx, cy = W / 2, H / 2
    dst = np.float32([[cx - pw / 2 + 60, cy - ph / 2], [cx + pw / 2 - 30, cy - ph / 2 + 40], [cx + pw / 2, cy + ph / 2], [cx - pw / 2, cy + ph / 2 - 20]])
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), dst)
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), M, (W, H))
    out = bg.copy(); out[mask > 0] = cv2.warpPerspective(page, M, (W, H))[mask > 0]
    out = (out * np.linspace(0.55, 1.0, W)[None, :, None]).astype(np.uint8)
    buf = io.BytesIO(); Image.fromarray(out).save(buf, "JPEG", quality=88)
    raw = buf.getvalue()
    r = extract.extract_invoice(*extract.prepare_image(raw), original_bytes=raw, photo=True)
    assert r.total == 345.0 and r.vat_amount == 45.0
    assert r.vendor_vat_number == "310455667700003"


def test_scan_document_crops_photo():
    import cv2
    W, H = 3000, 2200
    img = np.full((H, W, 3), (60, 80, 100), np.uint8)
    pts = np.array([[900, 300], [2000, 380], [1950, 1950], [850, 1880]], np.int32)
    cv2.fillPoly(img, [pts], (235, 235, 230))
    ok, buf = cv2.imencode(".jpg", img)
    out, cropped = ocr.scan_document(buf.tobytes())
    assert cropped
    res = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_GRAYSCALE)
    h, w = res.shape
    assert 1000 < w < 1300 and 1450 < h < 1700  # اتقصت الورقة بس
    assert res.mean() > 200  # من غير الخلفية الغامقة


def test_scan_document_manual_corners():
    import cv2
    img = np.full((1000, 1000, 3), 240, np.uint8)  # ورقة على ورق أبيض: مفيش حدود واضحة
    ok, buf = cv2.imencode(".jpg", img)
    out, cropped = ocr.scan_document(buf.tobytes(), [(0.1, 0.1), (0.6, 0.1), (0.6, 0.9), (0.1, 0.9)])
    res = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert cropped and abs(res.shape[1] - 500) < 15 and abs(res.shape[0] - 800) < 15
