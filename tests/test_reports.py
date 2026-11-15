"""صفحة التقارير والتصنيفات والأقسام."""


def test_lists_and_reports(admin):
    r = admin.post("/admin/lists/departments", data={"action": "add", "name": "قسم تجربة"})
    assert "قسم تجربة" in r.text
    r = admin.post("/admin/lists/categories", data={"action": "add", "name": "إعلانات"})
    assert "إعلانات" in r.text
    assert admin.post("/admin/lists/nothing", data={"action": "add", "name": "x"}).status_code == 404
    for tab in ["summary", "custody", "cheapest", "exceptions", "invoices"]:
        r = admin.get(f"/admin/reports?tab={tab}&preset=this_year")
        assert r.status_code == 200, tab
    r = admin.get("/admin/reports?tab=summary&date_from=2020-01-01&date_to=2030-12-31&status=all")
    assert r.status_code == 200
    r = admin.get("/admin/reports/export.xlsx?preset=this_year")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_reports_admin_only(emp):
    assert emp.get("/admin/reports").status_code == 403
    assert emp.get("/admin/lists").status_code == 403


def test_custody_review(admin):
    r = admin.get("/admin/custody-review")
    assert r.status_code == 200
    assert admin.get("/admin/custody-review?q=أحمد").status_code == 200
    r = admin.get("/admin/custody-review?q=1", follow_redirects=False)
    assert r.status_code in (200, 303)
    assert admin.get("/admin/custody-review/1").status_code == 200
    assert admin.get("/admin/custody-review/99999").status_code == 404


def test_english_switch(admin):
    r = admin.get("/lang?to=en&next=/admin/reports", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/reports"
    r = admin.get("/admin/reports")
    assert 'dir="ltr"' in r.text and "Reports" in r.text and "Spending analysis" in r.text
    assert admin.get("/lang?to=ar&next=//evil.com", follow_redirects=False).headers["location"] == "/"
    r = admin.get("/admin/reports")
    assert 'dir="rtl"' in r.text and "التقارير" in r.text


def test_delete_empty_custody(admin):
    admin.post("/admin/custodies", data={"user_id": "1", "name": "عهدة للحذف", "weekly_amount": "100",
                                         "per_invoice_limit": "50"})
    from app import db as dbm
    with dbm.get_db() as db:
        cid = db.execute("SELECT id FROM custodies WHERE name='عهدة للحذف'").fetchone()[0]
    r = admin.post(f"/admin/custodies/{cid}", data={"action": "delete"})
    assert "اتحذفت العهدة" in r.text
    with dbm.get_db() as db:
        assert not db.execute("SELECT 1 FROM custodies WHERE id=?", (cid,)).fetchone()
