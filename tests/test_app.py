"""اختبارات أساسية للدورة كاملة: إعداد، رفع فاتورة بـ QR، تنبيهات، مراجعة، إقفال، تحليل."""
import base64
import io
import os
import tempfile

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

os.environ["CUSTODY_DATA_DIR"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from app import extract  # noqa: E402
from app.main import app  # noqa: E402


def tlv(name, vat, ts, total, tax):
    out = b""
    for tag, val in enumerate([name, vat, ts, total, tax], start=1):
        b = val.encode()
        out += bytes([tag, len(b)]) + b
    return base64.b64encode(out).decode()


def invoice_image(payload: str) -> bytes:
    qr = cv2.QRCodeEncoder.create().encode(payload)
    qr = cv2.resize(qr, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    qr = cv2.copyMakeBorder(qr, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    canvas = Image.new("RGB", (1000, 1400), "white")
    ImageDraw.Draw(canvas).text((50, 50), "INVOICE 1001", fill="black")
    canvas.paste(Image.fromarray(qr).convert("RGB"), (250, 500))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def test_tlv_parse():
    parsed = extract.parse_zatca_tlv(tlv("مؤسسة النور", "300000000000003", "2026-11-12T10:00:00Z", "230.00", "30.00"))
    assert parsed["vendor_name"] == "مؤسسة النور"
    assert parsed["total"] == "230.00"


def test_qr_from_photo():
    img = invoice_image(tlv("ورشة الأمان", "311111111100003", "2026-11-12T10:00:00Z", "575.00", "75.00"))
    res = extract.extract_invoice(*extract.prepare_image(img), original_bytes=img)
    assert res.sources == ["qr"]
    assert res.vendor_name == "ورشة الأمان"
    assert res.total == 575.0 and res.vat_amount == 75.0 and res.subtotal == 500.0
    assert res.invoice_date == "2026-11-12"


def upload(emp, payload):
    img = invoice_image(payload)
    r = emp.post("/upload", data={"custody_id": "1"}, files={"image": ("a.jpg", img, "image/jpeg")})
    assert r.status_code == 200
    return int(str(r.url).rstrip("/edit").split("/")[-1]), r


def test_full_cycle(admin, emp):
    inv_id, r = upload(emp, tlv("ورشة الأمان", "311111111100003", "2026-11-12T10:00:00Z", "575.00", "75.00"))
    assert "ورشة الأمان" in r.text and "575.0" in r.text
    r = emp.post(f"/invoices/{inv_id}/edit", data={
        "vendor_name": "ورشة الأمان", "vendor_vat_number": "311111111100003", "invoice_number": "1001",
        "invoice_date": "2026-11-12", "subtotal": "500", "vat_amount": "75", "total": "575",
        "category_id": "2", "item_description": ["تغيير زيت", "فلتر"], "item_quantity": ["1", "2"],
        "item_unit_price": ["300", "100"], "item_line_total": ["", ""], "action": "submit"})
    assert "أكبر من الحد" in r.text

    # فاتورة تانية أرخص من مورد تاني لنفس الصنف
    inv2, _ = upload(emp, tlv("ورشة النجم", "322222222200003", "2026-11-13T10:00:00Z", "230.00", "30.00"))
    emp.post(f"/invoices/{inv2}/edit", data={
        "vendor_name": "ورشة النجم", "invoice_number": "77", "total": "230", "vat_amount": "30",
        "category_id": "2", "item_description": ["تغيير زيت"], "item_quantity": ["1"],
        "item_unit_price": ["200"], "item_line_total": [""], "action": "submit"})

    # نفس الفاتورة مرة تانية = مكررة
    inv3, _ = upload(emp, tlv("ورشة النجم", "322222222200003", "2026-11-13T10:00:00Z", "230.00", "30.00"))
    r = emp.post(f"/invoices/{inv3}/edit", data={"vendor_name": "ورشة النجم", "vendor_vat_number": "322222222200003",
                                                 "invoice_number": "77", "total": "230", "action": "submit"})
    assert "مكررة" in r.text

    r = admin.get("/admin/alerts")
    assert "تخطي حد الفاتورة" in r.text and "فاتورة مكررة" in r.text

    home = emp.get("/me").text
    assert "1,035.00" in home  # 575 + 230 + 230

    r = admin.get("/admin")
    assert r.status_code == 200 and "أحمد" in r.text

    # الإقفال مرفوض طول ما فيه فواتير مستنية
    r = admin.post("/admin/weeks/1/close", data={})
    assert "مستنية مراجعة" in r.text
    for i in (inv_id, inv2):
        admin.post(f"/admin/invoices/{i}/review", data={"decision": "approved"})
    admin.post(f"/admin/invoices/{inv3}/review", data={"decision": "rejected", "notes": "مكررة"})
    r = admin.get("/weeks/1")
    assert "805.00" in r.text  # المرفوضة مش محسوبة

    emp.post("/weeks/1/statement", files={"image": ("s.jpg", invoice_image("x"), "image/jpeg")})
    r = admin.post("/admin/weeks/1/close", data={"notes": "تمام"})
    assert "اتقفل الأسبوع" in r.text

    r = admin.get("/weeks/1/export.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert admin.get("/admin/export/invoices.xlsx").status_code == 200

    r = admin.get("/admin/vendors")
    assert "ورشة النجم" in r.text and "33%" in r.text  # 200 مقابل 300

    # أسبوع جديد يتفتح مع أول فاتورة بعد الإقفال
    inv4, _ = upload(emp, tlv("بقالة", "333333333300003", "2026-11-14T10:00:00Z", "11.50", "1.50"))
    r = emp.get("/me")
    assert "2,000.00" in r.text


def test_permissions(admin, emp):
    assert emp.get("/admin").status_code == 403
    other = TestClient(app)
    assert other.get("/invoices/1/image", follow_redirects=False).status_code == 303
    assert emp.get("/admin/settings").status_code == 403
    for path in ["/admin/invoices?status=all", "/admin/users", "/admin/custodies", "/admin/weeks?status=all",
                 "/admin/settings", "/admin/vendors/1", "/invoices/1", "/account"]:
        assert admin.get(path).status_code == 200, path


def test_claude_path(monkeypatch):
    """نتأكد إن رد Claude بيتدمج صح من غير ما نكلم الـ API فعلاً."""
    import json
    import anthropic

    class Block:
        type = "text"
        text = json.dumps({"is_invoice": True, "invoice_number": "INV-9", "vendor_name": "مكتبة جرير",
                           "vendor_vat_number": "300000000000003", "invoice_date": "2026-11-10",
                           "subtotal": 100, "vat_amount": 15, "total": 115, "category": "قرطاسية ومطبوعات",
                           "items": [{"description": "ورق A4", "quantity": 2, "unit_price": 50, "line_total": 100}],
                           "notes": ""})

    class Resp:
        stop_reason = "end_turn"
        content = [Block()]

    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return Resp()

    class FakeClient:
        def __init__(self, **kw):
            self.beta = type("B", (), {"messages": FakeMessages()})()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    img = Image.new("RGB", (400, 600), "white")
    buf = io.BytesIO(); img.save(buf, "JPEG")
    res = extract.extract_invoice(buf.getvalue(), "image/jpeg", api_key="k", categories=["قرطاسية ومطبوعات"])
    assert res.sources == ["ai"] and res.invoice_number == "INV-9" and res.total == 115
    assert res.items[0]["unit_price"] == 50
    assert captured["model"] == "claude-haiku-5-5" and "fallbacks" not in captured
    assert captured["output_config"]["format"]["type"] == "json_schema"
