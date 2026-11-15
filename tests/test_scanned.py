"""فواتير ممسوحة (CamScanner) وتعديل اسم المورد."""
import io

from app import db as dbm
from app import ocr, pdftext


def _scanned_pdf() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(img).text((50, 50), "INVOICE 123", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def test_scanner_marks_ignored():
    assert ocr.is_scanner_mark("Scanned with CamScanner")
    assert ocr.strip_scanner_marks("CamScanner\nمطبعة الذهبية") == "مطبعة الذهبية"
    data = ocr.parse_text("CamScanner\nمطبعة الطباعة الذهبية\nالإجمالي 140.00")
    assert data["vendor_name"] == "مطبعة الطباعة الذهبية"


def test_image_only_pdf_has_no_text():
    assert pdftext.extract_text(_scanned_pdf()) == ""


def test_grand_total_and_own_company():
    text = """شركة مصادر المملكة التجارية / Kingdom Resources
Riyadh
VAT Number: 300732463960603
شركة السحابة المحدودة
VAT ID 311281950700003
Tax Invoice INV/2026/14890
Invoice Date 30/10/2026
Total Before VatSR 52.17
Tctal After Dise 52.17 الاجمالي بعد الخصم
ضريبة القيمة 7.83
الإجمالي بعد الضريبة بالريال 60.00
"""
    data = ocr.parse_text(text, {"name": "مصادر المملكة Kingdom Resources", "vat": ""})
    assert data["total"] == 60.0
    assert data["vendor_vat_number"] == "311281950700003"
    assert data["vendor_name"] == "شركة السحابة المحدودة"
    assert data["invoice_number"] == "INV/2026/14890"


def test_vendor_rename_sticks():
    with dbm.get_db() as db:
        db.commit()
        db.execute("PRAGMA foreign_keys=OFF")
        vat = "399999999900093"
        junk = dbm.find_or_create_vendor(db, "CamScanner", vat)
        # اسم صح بنفس الرقم الضريبي بيصحح المورد اللي اسمه علامة مائية
        assert dbm.find_or_create_vendor(db, "CNTXT", vat) == junk
        assert db.execute("SELECT name FROM vendors WHERE id=?", (junk,)).fetchone()[0] == "CNTXT"
        # فاتورة تانية كانت مربوطة غلط بنفس المورد: الاسم اللي الموظف كتبه هو اللي يكسب
        db.execute("INSERT INTO invoices(week_id,custody_id,user_id,vendor_id,status,image_path) VALUES (0,0,0,?,'approved','')", (junk,))
        other = dbm.find_or_create_vendor(db, "مطبعة الذهبية", vat, invoice_id=-1, prefer_name=True)
        assert other != junk
        assert db.execute("SELECT name FROM vendors WHERE id=?", (other,)).fetchone()[0] == "مطبعة الذهبية"
        # وقت الرفع: رقم ضريبي متسجل على مورد اسمه مختلف خالص ميخليش الفاتورة تتربط بيه
        upload = dbm.find_or_create_vendor(db, "شركة نجوم الليزر", vat)
        assert upload not in (junk, other)
        # واسم قريب (قراءة مختلفة شوية) بيتربط بنفس المورد
        assert dbm.find_or_create_vendor(db, "CNTXT Company", vat) == junk
        db.rollback()


def test_own_vat_misread_is_ignored():
    text = "مورد الاختبار\nVAT 300738463950603\nالإجمالي 60.00"
    data = ocr.parse_text(text, {"name": "", "vat": "300732463960603"})
    assert "vendor_vat_number" not in data
