"""منصة مراقبة العهد والمصاريف."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import db as dbm
from . import pdftext, statement
from .alerts import KIND_LABELS, create_alert, run_invoice_checks
from .db import get_db, get_setting, set_setting
from .extract import DEFAULT_MODEL, extract_invoice, prepare_image

logging.basicConfig(level=logging.INFO)
APP_DIR = Path(__file__).resolve().parent

dbm.init_db()
with get_db() as _db:
    SECRET = get_setting(_db, "secret_key")

app = FastAPI(title="منصة مراقبة العهد")
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 24 * 14,
                   same_site="lax")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

STATUS_LABELS = {"draft": "مسودة", "pending": "بانتظار المراجعة",
                 "approved": "معتمدة", "rejected": "مرفوضة"}
SOURCE_LABELS = {"qr": "QR", "ai": "قراءة ذكية", "manual": "يدوي", "qr+ai": "QR + قراءة ذكية",
                 "ocr": "قراءة مجانية", "qr+ocr": "QR + قراءة مجانية",
                 "pdf": "نص الـ PDF", "qr+pdf": "QR + نص الـ PDF"}


def money(value) -> str:
    if value is None or value == "":
        return "-"
    return f"{float(value):,.2f}"


templates.env.filters["money"] = money
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, KIND_LABELS=KIND_LABELS,
                             SOURCE_LABELS=SOURCE_LABELS)


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
async def _redirect_handler(request: Request, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, message])


def current_user(request: Request, db):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()


def require_user(request: Request, db):
    user = current_user(request, db)
    if not user:
        raise Redirect("/login")
    return user


def require_admin(request: Request, db):
    user = require_user(request, db)
    if user["role"] != "admin":
        raise HTTPException(403, "الصفحة دي لمدير النظام بس")
    return user


def render(request: Request, db, name: str, **ctx):
    user = ctx.pop("user", None) or current_user(request, db)
    unread = 0
    has_custody = False
    if user and user["role"] == "admin":
        unread = db.execute("SELECT COUNT(*) FROM alerts WHERE is_read=0").fetchone()[0]
        has_custody = bool(db.execute("SELECT 1 FROM custodies WHERE user_id=? AND active=1",
                                      (user["id"],)).fetchone())
    messages = request.session.pop("flash", [])
    return templates.TemplateResponse(
        request, name,
        {"user": user, "unread_alerts": unread, "messages": messages, "has_custody": has_custody,
         "company": get_setting(db, "company_name", ""), **ctx},
    )


def to_float(value) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except ValueError:
        return None


def categories(db):
    return db.execute("SELECT * FROM categories ORDER BY id").fetchall()


# ======================= الدخول =======================
@app.get("/")
def home(request: Request):
    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            return go("/setup")
        user = current_user(request, db)
        if not user:
            return go("/login")
        return go("/admin" if user["role"] == "admin" else "/me")


@app.get("/setup")
def setup_form(request: Request):
    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return go("/login")
        return render(request, db, "setup.html")


@app.post("/setup")
def setup(request: Request, full_name: str = Form(...), username: str = Form(...),
          password: str = Form(...), company_name: str = Form("")):
    with get_db() as db:
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return go("/login")
        if len(password) < 6:
            flash(request, "كلمة السر لازم تكون 6 حروف على الأقل", "error")
            return go("/setup")
        cur = db.execute(
            "INSERT INTO users(username,full_name,role,password_hash) VALUES (?,?,?,?)",
            (username.strip().lower(), full_name.strip(), "admin", dbm.hash_password(password)),
        )
        set_setting(db, "company_name", company_name.strip())
        request.session["uid"] = cur.lastrowid
        flash(request, "تم إنشاء حساب المدير. ابدأ بإضافة الموظفين والعهد.")
        return go("/admin/users")


@app.get("/login")
def login_form(request: Request):
    with get_db() as db:
        return render(request, db, "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE username=? AND active=1",
                          (username.strip().lower(),)).fetchone()
        if not user or not dbm.verify_password(password, user["password_hash"]):
            flash(request, "اسم الدخول أو كلمة السر غلط", "error")
            return go("/login")
        request.session["uid"] = user["id"]
        return go("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/login")


@app.get("/account")
def account_form(request: Request):
    with get_db() as db:
        user = require_user(request, db)
        return render(request, db, "account.html", user=user)


@app.post("/account")
def account(request: Request, current: str = Form(...), new: str = Form(...)):
    with get_db() as db:
        user = require_user(request, db)
        if not dbm.verify_password(current, user["password_hash"]):
            flash(request, "كلمة السر الحالية غلط", "error")
        elif len(new) < 6:
            flash(request, "كلمة السر الجديدة لازم تكون 6 حروف على الأقل", "error")
        else:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (dbm.hash_password(new), user["id"]))
            flash(request, "اتغيرت كلمة السر")
        return go("/account")


# ======================= الموظف =======================
def user_custodies(db, user_id: int):
    rows = db.execute("SELECT * FROM custodies WHERE user_id=? AND active=1 ORDER BY id",
                      (user_id,)).fetchall()
    result = []
    for c in rows:
        week = dbm.get_or_open_week(db, c["id"])
        result.append({"custody": c, "week": week, "totals": dbm.week_totals(db, week["id"])})
    return result


@app.get("/me")
def employee_home(request: Request):
    with get_db() as db:
        user = require_user(request, db)
        custodies = user_custodies(db, user["id"])
        invoices = db.execute(
            """SELECT i.*, v.name AS vendor_name FROM invoices i
               LEFT JOIN vendors v ON v.id=i.vendor_id
               WHERE i.user_id=? ORDER BY i.id DESC LIMIT 30""",
            (user["id"],),
        ).fetchall()
        return render(request, db, "employee_home.html", user=user,
                      custodies=custodies, invoices=invoices)


@app.get("/upload")
def upload_form(request: Request, custody_id: int | None = None):
    with get_db() as db:
        user = require_user(request, db)
        custodies = db.execute("SELECT * FROM custodies WHERE user_id=? AND active=1",
                               (user["id"],)).fetchall()
        if not custodies:
            flash(request, "مفيش عهدة مسجلة باسمك. كلم مدير النظام.", "error")
            return go("/me")
        return render(request, db, "upload.html", user=user, custodies=custodies,
                      selected=custody_id or custodies[0]["id"])


def save_image(data: bytes, prefix: str) -> str:
    today = date.today()
    rel = Path(f"{today:%Y}") / f"{today:%m}" / f"{prefix}-{uuid.uuid4().hex[:12]}.jpg"
    path = dbm.UPLOAD_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel.as_posix()


def save_file(data: bytes, prefix: str, suffix: str) -> str:
    today = date.today()
    rel = Path(f"{today:%Y}") / f"{today:%m}" / f"{prefix}-{uuid.uuid4().hex[:12]}{suffix}"
    path = dbm.UPLOAD_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel.as_posix()


@app.post("/upload")
async def upload(request: Request, custody_id: int = Form(...), image: UploadFile = File(...)):
    raw = await image.read()
    if not raw:
        raise Redirect("/upload")
    with get_db() as db:
        user = require_user(request, db)
        custody = db.execute("SELECT * FROM custodies WHERE id=? AND user_id=? AND active=1",
                             (custody_id, user["id"])).fetchone()
        if not custody:
            raise HTTPException(404)
        pdf_text = ""
        image_bytes = raw
        if pdftext.is_pdf(raw):
            try:
                image_bytes = pdftext.render_first_page(raw)
                pdf_text = pdftext.extract_text(raw)
            except Exception:
                flash(request, "مقدرتش أفتح ملف الـ PDF ده.", "error")
                return go(f"/upload?custody_id={custody_id}")
        try:
            prepared, media_type = prepare_image(image_bytes)
        except Exception:
            flash(request, "الملف ده مش صورة ولا PDF. صوّر الفاتورة تاني.", "error")
            return go(f"/upload?custody_id={custody_id}")
        if pdf_text:
            save_file(raw, "inv", ".pdf")
        cats = [c["name"] for c in categories(db)]
        api_key = get_setting(db, "anthropic_api_key")
        model = get_setting(db, "anthropic_model", DEFAULT_MODEL)
        own = {"name": get_setting(db, "company_name", ""), "vat": get_setting(db, "company_vat_number", "")}
        week = dbm.get_or_open_week(db, custody_id)
        sha = hashlib.sha256(prepared).hexdigest()
        rel = save_image(prepared, "inv")
        db.commit()  # متمسكش قفل القاعدة أثناء القراءة الذكية

    ext = extract_invoice(prepared, media_type, api_key=api_key, model=model,
                          categories=cats, original_bytes=image_bytes, pdf_text=pdf_text,
                          own_company=own)

    with get_db() as db:
        vendor_id = dbm.find_or_create_vendor(db, ext.vendor_name, ext.vendor_vat_number)
        cat = db.execute("SELECT id FROM categories WHERE name=?", (ext.category,)).fetchone()
        cur = db.execute(
            """INSERT INTO invoices(week_id,custody_id,user_id,vendor_id,category_id,invoice_number,
                 invoice_date,subtotal,vat_amount,total,image_path,image_sha256,
                 extraction_source,extraction_raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (week["id"], custody_id, user["id"], vendor_id, cat["id"] if cat else None,
             ext.invoice_number, ext.invoice_date, ext.subtotal, ext.vat_amount, ext.total,
             rel, sha, ext.source_label,
             json.dumps({"raw": ext.raw, "notes": ext.notes}, ensure_ascii=False)),
        )
        inv_id = cur.lastrowid
        for it in ext.items:
            db.execute(
                "INSERT INTO invoice_items(invoice_id,description,quantity,unit_price,line_total)"
                " VALUES (?,?,?,?,?)",
                (inv_id, it["description"], it["quantity"], it["unit_price"], it["line_total"]),
            )
        for note in ext.notes:
            flash(request, note, "warn")
        if ext.sources:
            flash(request, "اتقرت بيانات الفاتورة. راجعها وعدّل أي غلط وبعدين ابعت.")
        return go(f"/invoices/{inv_id}/edit")


def load_invoice(db, invoice_id: int, user, for_edit: bool = False):
    inv = db.execute(
        """SELECT i.*, v.name AS vendor_name, v.vat_number AS vendor_vat, c.name AS custody_name,
                  c.per_invoice_limit, u.full_name, cat.name AS category_name, w.status AS week_status,
                  w.start_date, w.end_date, r.full_name AS reviewer_name
           FROM invoices i
           JOIN custodies c ON c.id=i.custody_id
           JOIN users u ON u.id=i.user_id
           JOIN weeks w ON w.id=i.week_id
           LEFT JOIN vendors v ON v.id=i.vendor_id
           LEFT JOIN categories cat ON cat.id=i.category_id
           LEFT JOIN users r ON r.id=i.reviewed_by
           WHERE i.id=?""",
        (invoice_id,),
    ).fetchone()
    if not inv:
        raise HTTPException(404, "الفاتورة مش موجودة")
    if user["role"] != "admin" and inv["user_id"] != user["id"]:
        raise HTTPException(403)
    if for_edit and user["role"] != "admin" and inv["status"] not in ("draft", "pending"):
        raise HTTPException(403, "الفاتورة اتراجعت ومينفعش تتعدل")
    return inv


@app.get("/invoices/{invoice_id}")
def invoice_detail(request: Request, invoice_id: int):
    with get_db() as db:
        user = require_user(request, db)
        inv = load_invoice(db, invoice_id, user)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice_id,)).fetchall()
        alerts = db.execute("SELECT * FROM alerts WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
        return render(request, db, "invoice_detail.html", user=user, inv=inv, items=items,
                      alerts=alerts)


@app.get("/invoices/{invoice_id}/edit")
def invoice_edit_form(request: Request, invoice_id: int):
    with get_db() as db:
        user = require_user(request, db)
        inv = load_invoice(db, invoice_id, user, for_edit=True)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice_id,)).fetchall()
        return render(request, db, "invoice_edit.html", user=user, inv=inv, items=items,
                      categories=categories(db))


@app.post("/invoices/{invoice_id}/edit")
async def invoice_edit(request: Request, invoice_id: int):
    form = await request.form()
    with get_db() as db:
        user = require_user(request, db)
        inv = load_invoice(db, invoice_id, user, for_edit=True)
        total = to_float(form.get("total"))
        vat = to_float(form.get("vat_amount"))
        subtotal = to_float(form.get("subtotal"))
        if subtotal is None and total is not None and vat is not None:
            subtotal = round(total - vat, 2)
        action = form.get("action", "save")
        if action == "submit" and (total is None or total <= 0):
            flash(request, "لازم تكتب إجمالي الفاتورة قبل ما تبعتها", "error")
            return go(f"/invoices/{invoice_id}/edit")
        vendor_id = dbm.find_or_create_vendor(db, form.get("vendor_name", ""),
                                              form.get("vendor_vat_number", ""),
                                              invoice_id=invoice_id, prefer_name=True)
        cat_id = form.get("category_id") or None
        week_id = inv["week_id"]
        if inv["week_status"] == "closed" and inv["status"] == "draft":
            week_id = dbm.get_or_open_week(db, inv["custody_id"])["id"]
        db.execute(
            """UPDATE invoices SET vendor_id=?, category_id=?, invoice_number=?, invoice_date=?,
                 subtotal=?, vat_amount=?, total=?, description=?, week_id=? WHERE id=?""",
            (vendor_id, cat_id, form.get("invoice_number", "").strip(),
             form.get("invoice_date", "").strip(), subtotal, vat, total,
             form.get("description", "").strip(), week_id, invoice_id),
        )
        db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
        descs = form.getlist("item_description")
        qtys = form.getlist("item_quantity")
        prices = form.getlist("item_unit_price")
        lines = form.getlist("item_line_total")
        for i, desc in enumerate(descs):
            if not desc.strip():
                continue
            q = to_float(qtys[i] if i < len(qtys) else None)
            p = to_float(prices[i] if i < len(prices) else None)
            lt = to_float(lines[i] if i < len(lines) else None)
            if lt is None and q is not None and p is not None:
                lt = round(q * p, 2)
            if p is None and lt is not None and q:
                p = round(lt / q, 2)
            db.execute(
                "INSERT INTO invoice_items(invoice_id,description,quantity,unit_price,line_total)"
                " VALUES (?,?,?,?,?)", (invoice_id, desc.strip(), q, p, lt))

        if action == "submit" and inv["status"] == "draft":
            db.execute("UPDATE invoices SET status='pending', submitted_at=? WHERE id=?",
                       (datetime.now().isoformat(timespec="seconds"), invoice_id))
            raised = run_invoice_checks(db, invoice_id)
            statement.match_week(db, week_id)
            if "over_limit" in raised:
                flash(request, "اتبعتت. الفاتورة أكبر من الحد المسموح، فاتبعت تنبيه للمدير.", "warn")
            elif "duplicate" in raised:
                flash(request, "اتبعتت، بس شكلها مكررة مع فاتورة قبل كده، فاتبعت تنبيه للمدير.", "warn")
            else:
                flash(request, "الفاتورة اتبعتت للمراجعة")
            return go("/me" if user["role"] != "admin" else f"/invoices/{invoice_id}")
        if inv["status"] in ("pending", "approved") and user["role"] == "admin":
            run_invoice_checks(db, invoice_id)
        flash(request, "اتحفظت التعديلات")
        return go(f"/invoices/{invoice_id}" if inv["status"] != "draft" else f"/invoices/{invoice_id}/edit")


@app.post("/invoices/{invoice_id}/delete")
def invoice_delete(request: Request, invoice_id: int):
    with get_db() as db:
        user = require_user(request, db)
        inv = load_invoice(db, invoice_id, user)
        if user["role"] != "admin" and inv["status"] != "draft":
            raise HTTPException(403, "ممكن تمسح المسودة بس")
        db.execute("UPDATE alerts SET invoice_id=NULL WHERE invoice_id=?", (invoice_id,))
        db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        (dbm.UPLOAD_DIR / inv["image_path"]).unlink(missing_ok=True)
        flash(request, "اتمسحت الفاتورة")
        return go("/me" if user["role"] != "admin" else "/admin/invoices")


@app.get("/invoices/{invoice_id}/image")
def invoice_image(request: Request, invoice_id: int):
    with get_db() as db:
        user = require_user(request, db)
        inv = load_invoice(db, invoice_id, user)
        path = (dbm.UPLOAD_DIR / inv["image_path"]).resolve()
    if not path.is_relative_to(dbm.UPLOAD_DIR.resolve()) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ======================= الأسابيع =======================
def load_week(db, week_id: int, user):
    week = db.execute(
        """SELECT w.*, c.name AS custody_name, c.user_id, c.per_invoice_limit, u.full_name,
                  cb.full_name AS closed_by_name
           FROM weeks w JOIN custodies c ON c.id=w.custody_id JOIN users u ON u.id=c.user_id
           LEFT JOIN users cb ON cb.id=w.closed_by WHERE w.id=?""",
        (week_id,),
    ).fetchone()
    if not week:
        raise HTTPException(404)
    if user["role"] != "admin" and week["user_id"] != user["id"]:
        raise HTTPException(403)
    return week


def week_invoices(db, week_id: int):
    return db.execute(
        """SELECT i.*, v.name AS vendor_name, v.vat_number, cat.name AS category_name
           FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id
           LEFT JOIN categories cat ON cat.id=i.category_id
           WHERE i.week_id=? AND i.status<>'draft' ORDER BY i.invoice_date, i.id""",
        (week_id,),
    ).fetchall()


@app.get("/weeks/{week_id}")
def week_detail(request: Request, week_id: int):
    with get_db() as db:
        user = require_user(request, db)
        week = load_week(db, week_id, user)
        by_cat = db.execute(
            """SELECT COALESCE(cat.name,'بدون تصنيف') AS name, SUM(i.total) AS total, COUNT(*) AS n
               FROM invoices i LEFT JOIN categories cat ON cat.id=i.category_id
               WHERE i.week_id=? AND i.status IN ('pending','approved')
               GROUP BY cat.name ORDER BY total DESC""",
            (week_id,),
        ).fetchall()
        return render(request, db, "week_detail.html", user=user, week=week,
                      totals=dbm.week_totals(db, week_id), invoices=week_invoices(db, week_id),
                      by_cat=by_cat, rec=statement.reconciliation(db, week_id))


@app.post("/weeks/{week_id}/statement")
async def week_statement(request: Request, week_id: int, image: UploadFile = File(...)):
    """بيان العهدة: ملف Excel/CSV (أدق) أو صورة. السطور بتتقري وتتربط بالفواتير بالمبلغ."""
    raw = await image.read()
    name = (image.filename or "").lower()
    with get_db() as db:
        user = require_user(request, db)
        week = load_week(db, week_id, user)
        if week["status"] == "closed":
            raise HTTPException(400, "الأسبوع مقفول")
        if not raw:
            return go(f"/weeks/{week_id}")
        lines: list[dict] = []
        if name.endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
            try:
                lines = statement.parse_excel(raw)
            except Exception:
                flash(request, "مقدرتش أفتح ملف الـ Excel ده. احفظه بصيغة xlsx وجرب تاني.", "error")
                return go(f"/weeks/{week_id}")
            rel = save_file(raw, "stmt", ".xlsx")
            db.execute("UPDATE weeks SET statement_file=? WHERE id=?", (rel, week_id))
        elif pdftext.is_pdf(raw):
            rel = save_file(raw, "stmt", ".pdf")
            db.execute("UPDATE weeks SET statement_file=? WHERE id=?", (rel, week_id))
            try:
                lines = statement.parse_pdf(raw)
                if not lines:  # PDF ممسوح: نحوله صورة ونقراه
                    from .ocr import read_text
                    img = pdftext.render_first_page(raw)
                    db.execute("UPDATE weeks SET statement_image=? WHERE id=?",
                               (save_image(prepare_image(img, max_side=2400)[0], "stmt"), week_id))
                    text = read_text(img)
                    lines = statement.parse_ocr_text(text) if text else []
            except Exception:
                flash(request, "مقدرتش أفتح ملف الـ PDF ده.", "error")
                return go(f"/weeks/{week_id}")
        elif name.endswith(".csv"):
            lines = statement.parse_csv(raw)
            rel = save_file(raw, "stmt", ".csv")
            db.execute("UPDATE weeks SET statement_file=? WHERE id=?", (rel, week_id))
        else:
            try:
                prepared, _ = prepare_image(raw, max_side=2400)
            except Exception:
                flash(request, "الملف ده مش صورة ولا Excel", "error")
                return go(f"/weeks/{week_id}")
            rel = save_image(prepared, "stmt")
            db.execute("UPDATE weeks SET statement_image=? WHERE id=?", (rel, week_id))
            db.commit()
            from .ocr import read_text
            text = read_text(raw) or read_text(prepared)
            lines = statement.parse_ocr_text(text) if text else []

        db.execute("DELETE FROM statement_lines WHERE week_id=?", (week_id,))
        for n, ln in enumerate(lines, start=1):
            db.execute(
                "INSERT INTO statement_lines(week_id,line_no,line_date,description,amount,subtotal,vat,cost_center)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (week_id, n, ln.get("line_date", ""), ln["description"], ln["amount"],
                 ln.get("subtotal"), ln.get("vat"), ln.get("cost_center", "")),
            )
        result = statement.match_week(db, week_id)
        create_alert(db, "statement",
                     f"{week['full_name']} رفع بيان عهدة {week['custody_name']} لأسبوع {week['start_date']} "
                     f"({len(lines)} سطر، اتربط منهم {result['matched']}).",
                     week_id=week_id)
        if lines:
            flash(request, f"اتقرا {len(lines)} سطر من البيان، واتربط {result['matched']} منهم بفواتير. راجع المطابقة.")
        else:
            flash(request, "اترفع البيان بس مقدرتش أقرا سطوره. اكتب السطور في صفحة المطابقة.", "warn")
        return go(f"/weeks/{week_id}/reconcile")


@app.get("/weeks/{week_id}/statement")
def week_statement_image(request: Request, week_id: int):
    with get_db() as db:
        user = require_user(request, db)
        week = load_week(db, week_id, user)
    rel = week["statement_image"] if request.query_params.get("file") != "1" else week["statement_file"]
    if not rel:
        raise HTTPException(404)
    path = dbm.UPLOAD_DIR / rel
    return FileResponse(path, filename=path.name if path.suffix != ".jpg" else None)


@app.get("/weeks/{week_id}/reconcile")
def week_reconcile(request: Request, week_id: int):
    with get_db() as db:
        user = require_user(request, db)
        week = load_week(db, week_id, user)
        rec = statement.reconciliation(db, week_id)
        return render(request, db, "week_reconcile.html", user=user, week=week, rec=rec,
                      totals=dbm.week_totals(db, week_id))


@app.post("/weeks/{week_id}/reconcile")
async def week_reconcile_save(request: Request, week_id: int):
    form = await request.form()
    with get_db() as db:
        user = require_user(request, db)
        week = load_week(db, week_id, user)
        if week["status"] == "closed":
            raise HTTPException(400, "الأسبوع مقفول")
        valid = {r["id"] for r in db.execute(
            "SELECT id FROM invoices WHERE week_id=? AND status IN ('pending','approved')", (week_id,))}
        old = {r["id"]: r for r in db.execute("SELECT * FROM statement_lines WHERE week_id=?", (week_id,))}
        db.execute("DELETE FROM statement_lines WHERE week_id=?", (week_id,))
        descs, amounts = form.getlist("line_description"), form.getlist("line_amount")
        dates, links, ids = form.getlist("line_date"), form.getlist("line_invoice"), form.getlist("line_id")
        used: set[int] = set()
        n = 0
        for i, desc in enumerate(descs):
            amount = to_float(amounts[i] if i < len(amounts) else None)
            if not desc.strip() and not amount:
                continue
            n += 1
            prev = old.get(int(ids[i])) if i < len(ids) and ids[i].isdigit() else None
            keep = prev is not None and prev["amount"] == amount
            cur = db.execute(
                "INSERT INTO statement_lines(week_id,line_no,line_date,description,amount,subtotal,vat,cost_center)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (week_id, n, (dates[i] if i < len(dates) else "").strip(), desc.strip(), amount,
                 prev["subtotal"] if keep else None, prev["vat"] if keep else None,
                 prev["cost_center"] if prev else ""),
            )
            choice = links[i] if i < len(links) else ""
            if choice.isdigit() and int(choice) in valid and int(choice) not in used:
                how = "auto" if prev and prev["invoice_id"] == int(choice) and prev["match_status"] == "auto" else "manual"
                statement.link(db, cur.lastrowid, int(choice), how)
                used.add(int(choice))
        result = statement.match_week(db, week_id)
        flash(request, "اتحفظ البيان" + (f"، واتربط {result['matched']} سطر تلقائي" if result["matched"] else ""))
        return go(f"/weeks/{week_id}/reconcile")


@app.post("/admin/weeks/{week_id}/close")
def week_close(request: Request, week_id: int, notes: str = Form("")):
    with get_db() as db:
        admin = require_admin(request, db)
        week = load_week(db, week_id, admin)
        totals = dbm.week_totals(db, week_id)
        if totals["pending_count"]:
            flash(request, f"فيه {totals['pending_count']} فاتورة لسه مستنية مراجعة. اعتمدها أو ارفضها الأول.", "error")
            return go(f"/weeks/{week_id}")
        if week["status"] == "closed":
            return go(f"/weeks/{week_id}")
        db.execute(
            "UPDATE weeks SET status='closed', closed_at=?, closed_by=?, close_notes=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), admin["id"], notes.strip(), week_id),
        )
        flash(request, "اتقفل الأسبوع. أول فاتورة جديدة هتفتح أسبوع جديد بمبلغ العهدة كامل.")
        return go(f"/weeks/{week_id}")


@app.post("/admin/weeks/{week_id}/reopen")
def week_reopen(request: Request, week_id: int):
    with get_db() as db:
        admin = require_admin(request, db)
        week = load_week(db, week_id, admin)
        other = db.execute("SELECT id FROM weeks WHERE custody_id=? AND status='open' AND id<>?",
                           (week["custody_id"], week_id)).fetchone()
        if other:
            flash(request, "فيه أسبوع تاني مفتوح للعهدة دي. اقفله الأول.", "error")
        else:
            db.execute("UPDATE weeks SET status='open', closed_at=NULL WHERE id=?", (week_id,))
            flash(request, "اتفتح الأسبوع تاني")
        return go(f"/weeks/{week_id}")


# ======================= Excel =======================
def xlsx_response(wb, filename: str) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


INVOICE_HEADERS = ["#", "التاريخ", "رقم الفاتورة", "المورد", "الرقم الضريبي", "التصنيف", "البيان",
                   "قبل الضريبة", "الضريبة", "الإجمالي", "الحالة", "فوق الحد"]


def invoice_row(i):
    return [i["id"], i["invoice_date"], i["invoice_number"], i["vendor_name"], i["vat_number"],
            i["category_name"], i["description"], i["subtotal"], i["vat_amount"], i["total"],
            STATUS_LABELS[i["status"]], "نعم" if i["over_limit"] else ""]


def new_sheet(wb, title):
    from openpyxl.styles import Font
    ws = wb.active if wb.active and wb.active.max_row == 1 and wb.active.title == "Sheet" else wb.create_sheet()
    ws.title = title
    ws.sheet_view.rightToLeft = True
    ws.bold = Font(bold=True)
    return ws


@app.get("/weeks/{week_id}/export.xlsx")
def week_export(request: Request, week_id: int):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    with get_db() as db:
        user = require_user(request, db)
        week = load_week(db, week_id, user)
        totals = dbm.week_totals(db, week_id)
        invoices = week_invoices(db, week_id)
    wb = Workbook()
    ws = new_sheet(wb, "ملخص الأسبوع")
    rows = [("الموظف", week["full_name"]), ("العهدة", week["custody_name"]),
            ("من", week["start_date"]), ("إلى", week["end_date"]),
            ("مبلغ العهدة", totals["opening"]), ("المصروف", totals["spent"]),
            ("منها ضريبة", totals["vat"]), ("المتبقي", totals["remaining"]),
            ("المرفوض", totals["rejected"]), ("عدد الفواتير", totals["count"]),
            ("الحالة", "مقفول" if week["status"] == "closed" else "مفتوح")]
    for r in rows:
        ws.append(r)
        ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 28
    ws2 = new_sheet(wb, "الفواتير")
    ws2.append(INVOICE_HEADERS)
    for c in ws2[1]:
        c.font = Font(bold=True)
    for i in invoices:
        ws2.append(invoice_row(i))
    ws3 = new_sheet(wb, "البيان والمطابقة")
    ws3.append(["#", "التاريخ", "البيان", "المبلغ", "الفاتورة", "المورد", "إجمالي الفاتورة", "الربط"])
    for c in ws3[1]:
        c.font = Font(bold=True)
    labels = {"auto": "بالمبلغ", "manual": "يدوي", "ambiguous": "محتاج اختيار", "none": "مفيش فاتورة"}
    with get_db() as db:
        rec = statement.reconciliation(db, week_id)
    for ln in rec["lines"]:
        ws3.append([ln["line_no"], ln["line_date"], ln["description"], ln["amount"], ln["invoice_id"],
                    ln["vendor_name"], ln["inv_total"], labels.get(ln["match_status"], "")])
    for i in rec["unmatched_invoices"]:
        ws3.append(["", i["invoice_date"], "فاتورة مش في البيان", None, i["id"], i["vendor_name"], i["total"], "مفيش سطر"])
    return xlsx_response(wb, f"week-{week_id}-{week['start_date']}.xlsx")


@app.get("/statement-template.xlsx")
def statement_template(request: Request):
    """قالب بيان العهدة بنفس شكل نموذج الشركة: الموظف بيملاه ويرفعه Excel أو PDF."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    with get_db() as db:
        user = require_user(request, db)
    wb = Workbook()
    ws = new_sheet(wb, "عهدة")
    ws.append(["التاريخ:-", date.today().isoformat()])
    ws.append(["اسم مسئول العهدة:", user["full_name"]])
    ws.append([])
    header = ["اجمالي القيمة", "الضريبة", "قيمة المبلغ", "بيان 1", "بيان 2", "مركز التكلفة", "التاريخ"]
    ws.append(header)
    for c in ws[4]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E3F2EF")
    first, last = 5, 24
    for r in range(first, last + 1):
        ws.cell(r, 1, f'=IF(C{r}="","",C{r}+B{r})')
    ws.cell(last + 1, 1, f"=SUM(A{first}:A{last})")
    ws.cell(last + 1, 2, f"=SUM(B{first}:B{last})")
    ws.cell(last + 1, 3, f"=SUM(C{first}:C{last})")
    ws.cell(last + 1, 4, "الاجماليات").font = Font(bold=True)
    for col, width in zip("ABCDEFG", (14, 10, 12, 18, 40, 14, 12)):
        ws.column_dimensions[col].width = width
    return xlsx_response(wb, "bayan-template.xlsx")


@app.get("/admin/export/invoices.xlsx")
def invoices_export(request: Request, date_from: str = "", date_to: str = ""):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    with get_db() as db:
        require_admin(request, db)
        where, params = ["i.status<>'draft'"], []
        if date_from:
            where.append("i.invoice_date>=?")
            params.append(date_from)
        if date_to:
            where.append("i.invoice_date<=?")
            params.append(date_to)
        rows = db.execute(
            f"""SELECT i.*, v.name AS vendor_name, v.vat_number, cat.name AS category_name,
                       u.full_name, c.name AS custody_name
                FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id
                LEFT JOIN categories cat ON cat.id=i.category_id
                JOIN users u ON u.id=i.user_id JOIN custodies c ON c.id=i.custody_id
                WHERE {' AND '.join(where)} ORDER BY i.invoice_date, i.id""",
            params,
        ).fetchall()
    wb = Workbook()
    ws = new_sheet(wb, "الفواتير")
    ws.append(["الموظف", "العهدة"] + INVOICE_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for i in rows:
        ws.append([i["full_name"], i["custody_name"]] + invoice_row(i))
    return xlsx_response(wb, f"invoices-{date.today()}.xlsx")


# ======================= المدير =======================
@app.get("/admin")
def admin_dashboard(request: Request):
    with get_db() as db:
        admin = require_admin(request, db)
        custodies = db.execute(
            """SELECT c.*, u.full_name FROM custodies c JOIN users u ON u.id=c.user_id
               WHERE c.active=1 ORDER BY u.full_name"""
        ).fetchall()
        rows = []
        for c in custodies:
            week = db.execute("SELECT * FROM weeks WHERE custody_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                              (c["id"],)).fetchone()
            rows.append({"custody": c, "week": week,
                         "totals": dbm.week_totals(db, week["id"]) if week else None})
        open_spent = sum(r["totals"]["spent"] for r in rows if r["totals"])
        open_amount = sum(r["totals"]["opening"] for r in rows if r["totals"])
        pending = db.execute("SELECT COUNT(*) FROM invoices WHERE status='pending'").fetchone()[0]
        over_limit = db.execute(
            "SELECT COUNT(*) FROM invoices WHERE status='pending' AND over_limit=1").fetchone()[0]
        alerts = db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 8").fetchall()
        month_start = date.today().replace(day=1).isoformat()
        by_cat = db.execute(
            """SELECT COALESCE(cat.name,'بدون تصنيف') AS name, SUM(i.total) AS total
               FROM invoices i LEFT JOIN categories cat ON cat.id=i.category_id
               WHERE i.status IN ('pending','approved') AND i.created_at>=?
               GROUP BY cat.name ORDER BY total DESC""",
            (month_start,),
        ).fetchall()
        return render(request, db, "admin_dashboard.html", user=admin, rows=rows,
                      open_spent=open_spent, open_amount=open_amount, pending=pending,
                      over_limit=over_limit, alerts=alerts, by_cat=by_cat,
                      month_total=sum(r["total"] or 0 for r in by_cat),
                      ai_enabled=bool(get_setting(db, "anthropic_api_key")),
                      ocr_ok=__import__("app.ocr", fromlist=["status"]).status()["installed"])


@app.get("/admin/invoices")
def admin_invoices(request: Request, status: str = "pending", user_id: str = "",
                   category_id: str = "", q: str = "", over: str = ""):
    with get_db() as db:
        admin = require_admin(request, db)
        where, params = ["i.status<>'draft'"], []
        if status and status != "all":
            where.append("i.status=?")
            params.append(status)
        if user_id:
            where.append("i.user_id=?")
            params.append(user_id)
        if category_id:
            where.append("i.category_id=?")
            params.append(category_id)
        if over:
            where.append("i.over_limit=1")
        if q:
            where.append("(i.invoice_number LIKE ? OR v.name LIKE ? OR v.vat_number LIKE ?)")
            params += [f"%{q}%"] * 3
        invoices = db.execute(
            f"""SELECT i.*, v.name AS vendor_name, u.full_name, cat.name AS category_name
                FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id
                JOIN users u ON u.id=i.user_id LEFT JOIN categories cat ON cat.id=i.category_id
                WHERE {' AND '.join(where)} ORDER BY i.id DESC LIMIT 300""",
            params,
        ).fetchall()
        users = db.execute("SELECT id, full_name FROM users WHERE role='employee' ORDER BY full_name").fetchall()
        return render(request, db, "admin_invoices.html", user=admin, invoices=invoices,
                      users=users, categories=categories(db),
                      f={"status": status, "user_id": user_id, "category_id": category_id,
                         "q": q, "over": over})


@app.post("/admin/invoices/{invoice_id}/review")
def admin_review(request: Request, invoice_id: int, decision: str = Form(...),
                 notes: str = Form(""), next: str = Form("")):
    if decision not in ("approved", "rejected", "pending"):
        raise HTTPException(400)
    with get_db() as db:
        admin = require_admin(request, db)
        inv = load_invoice(db, invoice_id, admin)
        if inv["week_status"] == "closed":
            flash(request, "الأسبوع ده مقفول. افتحه الأول لو عاوز تغير القرار.", "error")
            return go(f"/invoices/{invoice_id}")
        db.execute(
            "UPDATE invoices SET status=?, review_notes=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (decision, notes.strip(), admin["id"], datetime.now().isoformat(timespec="seconds"), invoice_id),
        )
        db.execute("UPDATE alerts SET is_read=1 WHERE invoice_id=?", (invoice_id,))
        flash(request, {"approved": "اتعتمدت الفاتورة", "rejected": "اترفضت الفاتورة",
                        "pending": "رجعت للمراجعة"}[decision])
        return go(next if next.startswith("/") else f"/invoices/{invoice_id}")


@app.get("/admin/users")
def admin_users(request: Request):
    with get_db() as db:
        admin = require_admin(request, db)
        users = db.execute(
            """SELECT u.*, (SELECT COUNT(*) FROM custodies c WHERE c.user_id=u.id AND c.active=1) AS n_custodies
               FROM users u ORDER BY u.role, u.full_name"""
        ).fetchall()
        return render(request, db, "admin_users.html", user=admin, users=users)


@app.post("/admin/users")
def admin_user_create(request: Request, full_name: str = Form(...), username: str = Form(...),
                      password: str = Form(...), role: str = Form("employee"),
                      phone: str = Form(""), email: str = Form(""),
                      weekly_amount: str = Form(""), per_invoice_limit: str = Form("")):
    with get_db() as db:
        require_admin(request, db)
        username = username.strip().lower()
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            flash(request, "اسم الدخول ده مستخدم قبل كده", "error")
            return go("/admin/users")
        if len(password) < 6:
            flash(request, "كلمة السر لازم تكون 6 حروف على الأقل", "error")
            return go("/admin/users")
        cur = db.execute(
            "INSERT INTO users(username,full_name,phone,email,role,password_hash) VALUES (?,?,?,?,?,?)",
            (username, full_name.strip(), phone.strip(), email.strip(),
             role if role in ("admin", "employee") else "employee", dbm.hash_password(password)),
        )
        amount, limit = to_float(weekly_amount), to_float(per_invoice_limit)
        if amount and limit:
            db.execute(
                "INSERT INTO custodies(user_id,name,weekly_amount,per_invoice_limit) VALUES (?,?,?,?)",
                (cur.lastrowid, f"عهدة {full_name.strip()}", amount, limit),
            )
        flash(request, f"اتضاف {full_name}")
        return go("/admin/users")


@app.post("/admin/users/{user_id}")
def admin_user_update(request: Request, user_id: int, action: str = Form(...),
                      full_name: str = Form(""), phone: str = Form(""), email: str = Form(""),
                      password: str = Form("")):
    with get_db() as db:
        admin = require_admin(request, db)
        if action == "update":
            db.execute("UPDATE users SET full_name=?, phone=?, email=? WHERE id=?",
                       (full_name.strip(), phone.strip(), email.strip(), user_id))
            flash(request, "اتحفظت البيانات")
        elif action == "password":
            if len(password) < 6:
                flash(request, "كلمة السر لازم تكون 6 حروف على الأقل", "error")
            else:
                db.execute("UPDATE users SET password_hash=? WHERE id=?",
                           (dbm.hash_password(password), user_id))
                flash(request, "اتغيرت كلمة السر")
        elif action == "toggle":
            if user_id == admin["id"]:
                flash(request, "مينفعش توقف حسابك", "error")
            else:
                db.execute("UPDATE users SET active=1-active WHERE id=?", (user_id,))
                flash(request, "اتغيرت حالة الحساب")
        return go("/admin/users")


@app.get("/admin/custodies")
def admin_custodies(request: Request):
    with get_db() as db:
        admin = require_admin(request, db)
        custodies = db.execute(
            """SELECT c.*, u.full_name FROM custodies c JOIN users u ON u.id=c.user_id
               ORDER BY c.active DESC, u.full_name"""
        ).fetchall()
        users = db.execute("SELECT id, full_name FROM users WHERE active=1 ORDER BY full_name").fetchall()
        return render(request, db, "admin_custodies.html", user=admin, custodies=custodies, users=users)


@app.post("/admin/custodies")
def admin_custody_create(request: Request, user_id: int = Form(...), name: str = Form(...),
                         weekly_amount: str = Form(...), per_invoice_limit: str = Form(...)):
    with get_db() as db:
        require_admin(request, db)
        amount, limit = to_float(weekly_amount), to_float(per_invoice_limit)
        if not amount or not limit:
            flash(request, "اكتب مبلغ العهدة والحد بالأرقام", "error")
            return go("/admin/custodies")
        db.execute("INSERT INTO custodies(user_id,name,weekly_amount,per_invoice_limit) VALUES (?,?,?,?)",
                   (user_id, name.strip(), amount, limit))
        flash(request, "اتضافت العهدة")
        return go("/admin/custodies")


@app.post("/admin/custodies/{custody_id}")
def admin_custody_update(request: Request, custody_id: int, action: str = Form("update"),
                         name: str = Form(""), weekly_amount: str = Form(""),
                         per_invoice_limit: str = Form("")):
    with get_db() as db:
        require_admin(request, db)
        if action == "toggle":
            db.execute("UPDATE custodies SET active=1-active WHERE id=?", (custody_id,))
        else:
            amount, limit = to_float(weekly_amount), to_float(per_invoice_limit)
            if not amount or not limit:
                flash(request, "اكتب مبلغ العهدة والحد بالأرقام", "error")
                return go("/admin/custodies")
            db.execute("UPDATE custodies SET name=?, weekly_amount=?, per_invoice_limit=? WHERE id=?",
                       (name.strip(), amount, limit, custody_id))
            db.execute("UPDATE weeks SET opening_amount=? WHERE custody_id=? AND status='open'",
                       (amount, custody_id))
        flash(request, "اتحفظت العهدة")
        return go("/admin/custodies")


@app.get("/admin/weeks")
def admin_weeks(request: Request, status: str = "open"):
    with get_db() as db:
        admin = require_admin(request, db)
        where = "" if status == "all" else "WHERE w.status=?"
        params = () if status == "all" else (status,)
        weeks = db.execute(
            f"""SELECT w.*, c.name AS custody_name, u.full_name FROM weeks w
                JOIN custodies c ON c.id=w.custody_id JOIN users u ON u.id=c.user_id
                {where} ORDER BY w.start_date DESC, u.full_name LIMIT 300""",
            params,
        ).fetchall()
        rows = [{"week": w, "totals": dbm.week_totals(db, w["id"])} for w in weeks]
        return render(request, db, "admin_weeks.html", user=admin, rows=rows, status=status)


@app.get("/admin/vendors")
def admin_vendors(request: Request, category_id: str = "", q: str = ""):
    with get_db() as db:
        admin = require_admin(request, db)
        cat_filter, params = "", []
        if category_id:
            cat_filter = "AND i.category_id=?"
            params.append(category_id)
        by_category = db.execute(
            f"""SELECT COALESCE(cat.name,'بدون تصنيف') AS category, v.id AS vendor_id, v.name AS vendor,
                       COUNT(*) AS n, SUM(i.total) AS total, AVG(i.total) AS avg_total,
                       MIN(i.total) AS min_total, MAX(i.invoice_date) AS last_date
                FROM invoices i JOIN vendors v ON v.id=i.vendor_id
                LEFT JOIN categories cat ON cat.id=i.category_id
                WHERE i.status IN ('pending','approved') {cat_filter}
                GROUP BY cat.name, v.id ORDER BY cat.name, avg_total""",
            params,
        ).fetchall()
        grouped: dict[str, list] = {}
        for r in by_category:
            grouped.setdefault(r["category"], []).append(r)

        item_params = list(params)
        item_q = ""
        if q:
            item_q = "AND it.description LIKE ?"
            item_params.append(f"%{q}%")
        item_rows = db.execute(
            f"""SELECT LOWER(TRIM(it.description)) AS item_key, it.description, v.id AS vendor_id,
                       v.name AS vendor, it.unit_price, i.invoice_date
                FROM invoice_items it JOIN invoices i ON i.id=it.invoice_id
                JOIN vendors v ON v.id=i.vendor_id
                WHERE i.status IN ('pending','approved') AND it.unit_price IS NOT NULL
                  AND it.unit_price>0 {cat_filter} {item_q}""",
            item_params,
        ).fetchall()
        items: dict[str, dict] = {}
        for r in item_rows:
            entry = items.setdefault(r["item_key"], {"name": r["description"], "vendors": {}})
            vend = entry["vendors"].setdefault(r["vendor_id"], {"vendor": r["vendor"], "prices": []})
            vend["prices"].append(r["unit_price"])
        comparisons = []
        for entry in items.values():
            vendors = [{"vendor": v["vendor"], "min": min(v["prices"]),
                        "avg": sum(v["prices"]) / len(v["prices"]), "n": len(v["prices"])}
                       for v in entry["vendors"].values()]
            vendors.sort(key=lambda x: x["avg"])
            cheapest, priciest = vendors[0], vendors[-1]
            comparisons.append({
                "name": entry["name"], "vendors": vendors, "cheapest": cheapest,
                "saving_pct": (1 - cheapest["avg"] / priciest["avg"]) * 100 if len(vendors) > 1 else 0,
                "n": sum(v["n"] for v in vendors),
            })
        comparisons.sort(key=lambda c: (-(len(c["vendors"]) > 1), -c["saving_pct"], -c["n"]))
        return render(request, db, "admin_vendors.html", user=admin, grouped=grouped,
                      comparisons=comparisons[:100], categories=categories(db),
                      f={"category_id": category_id, "q": q})


@app.get("/admin/vendors/{vendor_id}")
def admin_vendor_detail(request: Request, vendor_id: int):
    with get_db() as db:
        admin = require_admin(request, db)
        vendor = db.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
        if not vendor:
            raise HTTPException(404)
        invoices = db.execute(
            """SELECT i.*, u.full_name, cat.name AS category_name FROM invoices i
               JOIN users u ON u.id=i.user_id LEFT JOIN categories cat ON cat.id=i.category_id
               WHERE i.vendor_id=? AND i.status<>'draft' ORDER BY i.id DESC""",
            (vendor_id,),
        ).fetchall()
        return render(request, db, "admin_vendor_detail.html", user=admin, vendor=vendor,
                      invoices=invoices)


@app.post("/admin/vendors/{vendor_id}")
def admin_vendor_update(request: Request, vendor_id: int, name: str = Form(...),
                        vat_number: str = Form(""), merge_into: str = Form("")):
    with get_db() as db:
        require_admin(request, db)
        target = to_float(merge_into)
        if target and int(target) != vendor_id:
            db.execute("UPDATE invoices SET vendor_id=? WHERE vendor_id=?", (int(target), vendor_id))
            db.execute("DELETE FROM vendors WHERE id=?", (vendor_id,))
            flash(request, "اتدمج المورد")
            return go(f"/admin/vendors/{int(target)}")
        db.execute("UPDATE vendors SET name=?, vat_number=? WHERE id=?",
                   (dbm.normalize_vendor_name(name), vat_number.strip(), vendor_id))
        flash(request, "اتحفظ المورد")
        return go(f"/admin/vendors/{vendor_id}")


@app.get("/admin/alerts")
def admin_alerts(request: Request, show: str = "unread"):
    with get_db() as db:
        admin = require_admin(request, db)
        where = "WHERE a.is_read=0" if show == "unread" else ""
        alerts = db.execute(
            f"""SELECT a.*, i.status AS invoice_status FROM alerts a
                LEFT JOIN invoices i ON i.id=a.invoice_id {where} ORDER BY a.id DESC LIMIT 300"""
        ).fetchall()
        return render(request, db, "admin_alerts.html", user=admin, alerts=alerts, show=show)


@app.post("/admin/alerts/read")
def admin_alerts_read(request: Request, alert_id: str = Form("")):
    with get_db() as db:
        require_admin(request, db)
        if alert_id:
            db.execute("UPDATE alerts SET is_read=1 WHERE id=?", (alert_id,))
        else:
            db.execute("UPDATE alerts SET is_read=1")
        return go("/admin/alerts")


SETTING_KEYS = ["company_name", "company_vat_number", "anthropic_api_key", "anthropic_model", "alert_email",
                "smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from", "smtp_tls",
                "week_start_weekday"]


@app.get("/admin/settings")
def admin_settings(request: Request):
    with get_db() as db:
        admin = require_admin(request, db)
        values = {k: get_setting(db, k) for k in SETTING_KEYS}
        values["anthropic_model"] = values["anthropic_model"] or DEFAULT_MODEL
        values["week_start_weekday"] = values["week_start_weekday"] or "6"
        values["smtp_tls"] = values["smtp_tls"] or "1"
        from .ocr import status as ocr_status
        return render(request, db, "admin_settings.html", user=admin, v=values,
                      categories=categories(db), ocr=ocr_status())


@app.post("/admin/settings")
async def admin_settings_save(request: Request):
    form = await request.form()
    with get_db() as db:
        require_admin(request, db)
        for key in SETTING_KEYS:
            if key in form:
                value = str(form.get(key, "")).strip()
                if key in ("anthropic_api_key", "smtp_password") and value == "********":
                    continue
                set_setting(db, key, value)
        flash(request, "اتحفظت الإعدادات")
        return go("/admin/settings")


@app.post("/admin/categories")
def admin_category(request: Request, name: str = Form(""), rename_id: str = Form(""),
                   new_name: str = Form("")):
    with get_db() as db:
        require_admin(request, db)
        if rename_id and new_name.strip():
            db.execute("UPDATE categories SET name=? WHERE id=?", (new_name.strip(), rename_id))
        elif name.strip():
            db.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name.strip(),))
        flash(request, "اتحفظت التصنيفات")
        return go("/admin/settings")


@app.get("/health")
def health():
    return {"ok": True}
