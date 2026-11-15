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
