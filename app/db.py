"""قاعدة البيانات: SQLite في ملف واحد داخل فولدر data."""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CUSTODY_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "custody.db"

DEFAULT_CATEGORIES = [
    "مشتريات عامة",
    "صيانة سيارات",
    "صيانة مباني ومعدات",
    "وقود",
    "قرطاسية ومطبوعات",
    "ضيافة ونظافة",
    "نقل وشحن",
    "أخرى",
]

DEFAULT_DEPARTMENTS = ["الإدارة", "التسويق", "المبيعات", "المشتريات", "الصيانة", "المالية", "الموارد البشرية"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    email TEXT DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('admin','employee')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS custodies (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    weekly_amount REAL NOT NULL,
    per_invoice_limit REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS weeks (
    id INTEGER PRIMARY KEY,
    custody_id INTEGER NOT NULL REFERENCES custodies(id),
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    opening_amount REAL NOT NULL,
    statement_image TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    closed_at TEXT,
    closed_by INTEGER REFERENCES users(id),
    close_notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    vat_number TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    week_id INTEGER NOT NULL REFERENCES weeks(id),
    custody_id INTEGER NOT NULL REFERENCES custodies(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    vendor_id INTEGER REFERENCES vendors(id),
    category_id INTEGER REFERENCES categories(id),
    invoice_number TEXT DEFAULT '',
    invoice_date TEXT DEFAULT '',
    subtotal REAL,
    vat_amount REAL,
    total REAL,
    description TEXT DEFAULT '',
    image_path TEXT NOT NULL,
    image_sha256 TEXT,
    extraction_source TEXT DEFAULT 'manual',
    extraction_raw TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','pending','approved','rejected')),
    over_limit INTEGER NOT NULL DEFAULT 0,
    review_notes TEXT DEFAULT '',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    submitted_at TEXT
);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity REAL,
    unit_price REAL,
    line_total REAL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    invoice_id INTEGER REFERENCES invoices(id),
    week_id INTEGER REFERENCES weeks(id),
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS statement_lines (
    id INTEGER PRIMARY KEY,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    line_no INTEGER NOT NULL,
    line_date TEXT DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    amount REAL,
    subtotal REAL,
    vat REAL,
    cost_center TEXT DEFAULT '',
    invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    match_status TEXT NOT NULL DEFAULT 'none'
        CHECK (match_status IN ('none','auto','manual','ambiguous'))
);
CREATE INDEX IF NOT EXISTS ix_invoices_week ON invoices(week_id);
CREATE INDEX IF NOT EXISTS ix_statement_week ON statement_lines(week_id);
CREATE INDEX IF NOT EXISTS ix_invoices_vendor ON invoices(vendor_id);
CREATE INDEX IF NOT EXISTS ix_invoices_number ON invoices(invoice_number);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        week_cols = {r["name"] for r in db.execute("PRAGMA table_info(weeks)")}
        if "statement_file" not in week_cols:
            db.execute("ALTER TABLE weeks ADD COLUMN statement_file TEXT")
        line_cols = {r["name"] for r in db.execute("PRAGMA table_info(statement_lines)")}
        for col, typ in (("subtotal", "REAL"), ("vat", "REAL"), ("cost_center", "TEXT DEFAULT ''")):
            if col not in line_cols:
                db.execute(f"ALTER TABLE statement_lines ADD COLUMN {col} {typ}")
        for table in ("categories", "departments"):
            cols = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            if "active" not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        inv_cols = {r["name"] for r in db.execute("PRAGMA table_info(invoices)")}
        if "department_id" not in inv_cols:
            db.execute("ALTER TABLE invoices ADD COLUMN department_id INTEGER REFERENCES departments(id)")
        if "original_path" not in inv_cols:
            # ملف الـ PDF الأصلي بكل صفحاته (الصورة المعروضة هي أول صفحة بس)
            db.execute("ALTER TABLE invoices ADD COLUMN original_path TEXT NOT NULL DEFAULT ''")
        cust_cols = {r["name"] for r in db.execute("PRAGMA table_info(custodies)")}
        if "department_id" not in cust_cols:
            db.execute("ALTER TABLE custodies ADD COLUMN department_id INTEGER REFERENCES departments(id)")
        if not get_setting(db, "departments_seeded"):
            db.executemany("INSERT OR IGNORE INTO departments(name) VALUES (?)",
                           [(d,) for d in DEFAULT_DEPARTMENTS])
            set_setting(db, "departments_seeded", "1")
        if db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            db.executemany(
                "INSERT INTO categories(name) VALUES (?)",
                [(c,) for c in DEFAULT_CATEGORIES],
            )
        if not get_setting(db, "secret_key"):
            set_setting(db, "secret_key", secrets.token_hex(32))


# ---------- الإعدادات ----------
def get_setting(db, key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row and row["value"] is not None and row["value"] != "":
        return row["value"]
    return os.environ.get(key.upper(), default)


def set_setting(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def week_start_weekday(db) -> int:
    """يوم بداية الأسبوع (Python: الاثنين=0 ... الأحد=6). الافتراضي الأحد."""
    try:
        return int(get_setting(db, "week_start_weekday", "6"))
    except ValueError:
        return 6


# ---------- كلمات السر ----------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return secrets.compare_digest(check.hex(), digest)


# ---------- الأسابيع ----------
def current_week_bounds(db, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    start_wd = week_start_weekday(db)
    delta = (today.weekday() - start_wd) % 7
    start = today - timedelta(days=delta)
    return start, start + timedelta(days=6)


def get_or_open_week(db, custody_id: int) -> sqlite3.Row:
    """يرجع الأسبوع المفتوح للعهدة، ولو مفيش يفتح أسبوع جديد."""
    week = db.execute(
        "SELECT * FROM weeks WHERE custody_id=? AND status='open' ORDER BY id DESC LIMIT 1",
        (custody_id,),
    ).fetchone()
    if week:
        return week
    custody = db.execute("SELECT * FROM custodies WHERE id=?", (custody_id,)).fetchone()
    start, end = current_week_bounds(db)
    cur = db.execute(
        "INSERT INTO weeks(custody_id,start_date,end_date,opening_amount) VALUES (?,?,?,?)",
        (custody_id, start.isoformat(), end.isoformat(), custody["weekly_amount"]),
    )
    return db.execute("SELECT * FROM weeks WHERE id=?", (cur.lastrowid,)).fetchone()


def week_totals(db, week_id: int) -> dict:
    """المصروف يحسب الفواتير المعلقة والمعتمدة (المرفوضة والمسودات لأ)."""
    row = db.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN status IN ('pending','approved') THEN total END),0) AS spent,
             COALESCE(SUM(CASE WHEN status IN ('pending','approved') THEN vat_amount END),0) AS vat,
             COALESCE(SUM(CASE WHEN status='approved' THEN total END),0) AS approved,
             COALESCE(SUM(CASE WHEN status='pending' THEN total END),0) AS pending,
             COALESCE(SUM(CASE WHEN status='rejected' THEN total END),0) AS rejected,
             SUM(CASE WHEN status IN ('pending','approved','rejected') THEN 1 ELSE 0 END) AS count,
             SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_count
           FROM invoices WHERE week_id=?""",
        (week_id,),
    ).fetchone()
    week = db.execute("SELECT opening_amount FROM weeks WHERE id=?", (week_id,)).fetchone()
    data = dict(row)
    data["count"] = data["count"] or 0
    data["pending_count"] = data["pending_count"] or 0
    data["opening"] = week["opening_amount"]
    data["remaining"] = week["opening_amount"] - data["spent"]
    return data


# ---------- الموردين ----------
def normalize_vendor_name(name: str) -> str:
    return " ".join((name or "").replace("ـ", "").split()).strip()


def _junk_vendor_name(name: str, vat_number: str = "") -> bool:
    from .ocr import is_scanner_mark

    name = (name or "").strip()
    return not name or name == vat_number or is_scanner_mark(name)


def _similar_names(a: str, b: str) -> bool:
    """فيه كلمة مميزة مشتركة بين الاسمين (مش شركة/مؤسسة)."""
    from .ocr import _name_words

    return bool(_name_words(a) & _name_words(b))


def find_or_create_vendor(db, name: str, vat_number: str, invoice_id: int | None = None,
                          prefer_name: bool = False) -> int | None:
    """يلاقي المورد أو يعمله.
    prefer_name: الاسم اللي كتبه الموظف بإيده هو الأساس، حتى لو الرقم الضريبي بتاع مورد تاني.
    invoice_id: الفاتورة اللي بتتعدل، عشان لو هي الوحيدة على المورد نعدل اسمه بدل ما نعمل مورد جديد."""
    name = normalize_vendor_name(name)
    vat_number = (vat_number or "").strip()
    if not name and not vat_number:
        return None
    if vat_number:
        row = db.execute("SELECT id, name FROM vendors WHERE vat_number=? ORDER BY id",
                         (vat_number,)).fetchone()
        if row:
            same = not name or row["name"].casefold() == name.casefold()
            if same:
                return row["id"]
            others = db.execute(
                "SELECT COUNT(*) FROM invoices WHERE vendor_id=? AND id IS NOT ?",
                (row["id"], invoice_id)).fetchone()[0]
            if _junk_vendor_name(row["name"], vat_number) and not others:
                # اسم غلط زي CamScanner ومفيش فواتير تانية عليه: نصححه بالاسم الصح
                db.execute("UPDATE vendors SET name=? WHERE id=?", (name, row["id"]))
                return row["id"]
            if not prefer_name and _similar_names(row["name"], name):
                return row["id"]  # نفس المورد واسمه اتقري بشكل مختلف شوية
            # الاسم مختلف خالص: الرقم الضريبي غالباً اتقري غلط أو متسجل على مورد غلط، فالاسم هو الأساس
            by_name = db.execute("SELECT id FROM vendors WHERE name=? COLLATE NOCASE",
                                 (name,)).fetchone()
            if by_name:
                return by_name["id"]
            if not others:
                db.execute("UPDATE vendors SET name=? WHERE id=?", (name, row["id"]))
                return row["id"]
            # الرقم الضريبي بتاع مورد تاني عليه فواتير: نعمل المورد الجديد من غير الرقم ده
            vat_number = ""
    if name:
        row = db.execute(
            "SELECT id, vat_number FROM vendors WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone()
        if row:
            if vat_number and not row["vat_number"]:
                db.execute("UPDATE vendors SET vat_number=? WHERE id=?", (vat_number, row["id"]))
            return row["id"]
    cur = db.execute(
        "INSERT INTO vendors(name, vat_number) VALUES (?,?)", (name or vat_number, vat_number)
    )
    return cur.lastrowid
