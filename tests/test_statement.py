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
    assert "اتقرا 3 سطر" in r.text and "اتربط 2" in r.text
    r = emp.get(f"/weeks/{week_id}")
    assert "1 سطر في البيان مالوش فاتورة" in r.text
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
    assert "اتربط بالمبلغ" in page
    r = emp.post(f"/weeks/{week_id}/reconcile", data={
        "line_id": ["", "", ""], "line_description": ["تغيير زيت", "أدوات نظافة", "مواصلات"],
        "line_amount": ["500", "86.25", "40"], "line_date": ["", "", ""], "line_invoice": [str(a), str(b), ""]})
    assert r.status_code == 200
    # السطر التالت اتربط تاني تلقائي بالمبلغ
    assert "مربوطة بفواتير" in emp.get(f"/weeks/{week_id}").text


def test_template_roundtrip(emp):
    r = emp.get("/statement-template.xlsx")
    assert r.status_code == 200
    lines = statement.parse_excel(r.content)
    assert len(lines) == 1 and lines[0]["amount"] == 500
