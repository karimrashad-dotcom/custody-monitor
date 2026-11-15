"""بيان العهدة: القراءة من Excel والربط بالفواتير بالمبلغ."""
import io

from openpyxl import Workbook

from app import statement
from tests.test_app import tlv, upload


def make_xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["بيان عهدة أسبوعية"])
    ws.append([])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_rows_with_headers():
    rows = [["م", "التاريخ", "البيان", "المبلغ"], [1, "2026-11-15", "تغيير زيت سيارة النقل", 500],
            [2, "2026-11-16", "أدوات نظافة", "86.25"], [None, None, "الإجمالي", 586.25]]
    lines = statement.parse_rows(rows)
    assert [(l["description"], l["amount"]) for l in lines] == [("تغيير زيت سيارة النقل", 500.0), ("أدوات نظافة", 86.25)]
    assert lines[0]["line_date"] == "2026-11-15"


def test_parse_ocr_lines():
    text = "بيان العهدة\n١ تغيير زيت السيارة ٥٠٠\n2 أدوات نظافة 86.25\nالإجمالي 586.25"
    lines = statement.parse_ocr_text(text)
    assert [l["amount"] for l in lines] == [500.0, 86.25]


def test_statement_matching(admin, emp):
    # الأسبوع المفتوح دلوقتي هو التاني (الأول اتقفل في test_full_cycle)
    week_id = 2
    def submit(payload, **fields):
        inv_id, _ = upload(emp, payload)
        data = {"action": "submit", **fields}
        emp.post(f"/invoices/{inv_id}/edit", data=data)
        return inv_id
    a = submit(tlv("ورشة الأمان", "311111111100003", "2026-11-15T10:00:00Z", "575.00", "75.00"),
               vendor_name="ورشة الأمان", total="575", vat_amount="75", subtotal="500", invoice_date="2026-11-15")
    b = submit(tlv("سوبرماركت الدانة", "355555555500003", "2026-11-16T10:00:00Z", "86.25", "11.25"),
               vendor_name="سوبرماركت الدانة", total="86.25", vat_amount="11.25", invoice_date="2026-11-16")
    xlsx = make_xlsx([["التاريخ", "البيان", "المبلغ"], ["2026-11-15", "تغيير زيت سيارة النقل", 500],
                      ["2026-11-16", "أدوات نظافة", 86.25], ["2026-11-17", "مواصلات", 40], ["", "الإجمالي", 626.25]])
    r = emp.post(f"/weeks/{week_id}/statement", files={"image": ("bayan.xlsx", xlsx, "application/vnd.ms-excel")})
    assert "تمت قراءة 3 سطر" in r.text and "رُبط 2" in r.text
    r = emp.get(f"/weeks/{week_id}")
    assert "1 سطر في البيان ليس له فاتورة" in r.text
    inv = admin.get(f"/invoices/{a}").text
    assert "تغيير زيت سيارة النقل" in inv  # الوصف جه من البيان
    # فاتورة متأخرة بنفس مبلغ السطر الناقص تتربط تلقائي لما تتبعت
    c = submit(tlv("أوبر", "366666666600003", "2026-11-17T10:00:00Z", "40.00", "5.22"),
               vendor_name="أوبر", total="40", vat_amount="5.22", invoice_date="2026-11-17")
    r = emp.get(f"/weeks/{week_id}")
    assert "مربوطة بفواتير" in r.text
    # البيان مع الفواتير في Excel الأسبوع
    assert admin.get(f"/weeks/{week_id}/export.xlsx").status_code == 200
    # تعديل يدوي: نفك الربط من صفحة المطابقة
    page = emp.get(f"/weeks/{week_id}/reconcile").text
    assert "رُبط بالمبلغ" in page
    r = emp.post(f"/weeks/{week_id}/reconcile", data={
        "line_id": ["", "", ""], "line_description": ["تغيير زيت", "أدوات نظافة", "مواصلات"],
        "line_amount": ["500", "86.25", "40"], "line_date": ["", "", ""], "line_invoice": [str(a), str(b), ""]})
    assert r.status_code == 200
    # السطر التالت اتربط تاني تلقائي بالمبلغ
    assert "مربوطة بفواتير" in emp.get(f"/weeks/{week_id}").text


def test_template_roundtrip(emp):
    from openpyxl import load_workbook
    r = emp.get("/statement-template.xlsx")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    ws["C5"], ws["B5"], ws["E5"], ws["G5"] = 500, 75, "تغيير زيت", "2026-11-15"
    ws["C6"], ws["E6"] = 40, "انتقالات"
    buf = io.BytesIO(); wb.save(buf)
    lines = statement.parse_excel(buf.getvalue())
    assert [(l["description"], l["amount"], l["subtotal"]) for l in lines] == [("تغيير زيت", 575.0, 500.0), ("انتقالات", 40.0, 40.0)]


def test_company_format_and_pdf(tmp_path):
    """نفس شكل نموذج الشركة: بيان 1 فاضي والوصف في بيان 2، والمبلغ قبل وبعد الضريبة في أعمدة منفصلة."""
    import shutil
    import subprocess
    rows = [["اجمالي القيمة", "الضريبة", "قيمة المبلغ", "بيان 1", "بيان 2", "مركز التكلفة", "التاريخ"],
            [29.95, 3.91, 26.04, None, "شراء باسكت مهملات", None, "7/13/2026"],
            [172.5, None, 172.5, None, "انتقالات", None, "7/12/2026"],
            [0, None, None, None, None, None, None],
            [202.45, 3.91, 198.54, "الاجماليات", None, None, None]]
    lines = statement.parse_rows(rows)
    assert [(l["description"], l["amount"], l["subtotal"], l["line_date"]) for l in lines] == [
        ("شراء باسكت مهملات", 29.95, 26.04, "2026-07-13"), ("انتقالات", 172.5, 172.5, "2026-07-12")]
    soffice = shutil.which("soffice")
    if not soffice:
        return
    from openpyxl.styles import Border, Side
    wb = Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True
    thin = Side(style="thin")
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows():
        for c in row:
            c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col, w in zip("ABCDEFG", (12, 9, 11, 9, 28, 11, 11)):
        ws.column_dimensions[col].width = w
    ws.page_setup.orientation = "landscape"
    x = tmp_path / "bayan.xlsx"
    wb.save(x)
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(x)],
                   check=True, capture_output=True, timeout=120)
    pdf_lines = statement.parse_pdf((tmp_path / "bayan.pdf").read_bytes())
    assert [(l["description"], l["amount"], l["subtotal"]) for l in pdf_lines] == [
        ("شراء باسكت مهملات", 29.95, 26.04), ("انتقالات", 172.5, 172.5)]


def test_invoice_pdf_upload(emp):
    """فاتورة PDF (صورة جوه PDF): بتتحول صورة ويتقري الـ QR."""
    from PIL import Image
    from tests.test_app import invoice_image
    img = Image.open(io.BytesIO(invoice_image(tlv("محطة الوقود", "377777777700003", "2026-11-18T08:00:00Z", "115.00", "15.00"))))
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    r = emp.post("/upload", data={"custody_id": "1"}, files={"image": ("inv.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 200 and "محطة الوقود" in r.text and "115.0" in r.text
