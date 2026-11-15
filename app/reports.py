"""التقارير: كل التقارير بتتبني من نفس الفلاتر (التاريخ، الموظف، العهدة، التصنيف، القسم، المورد، الحالة)."""
from __future__ import annotations

from datetime import date, timedelta

# تاريخ الفاتورة، ولو مش مكتوب ناخد تاريخ الرفع
INV_DATE = "COALESCE(NULLIF(i.invoice_date,''), substr(i.created_at,1,10))"

STATUS_SETS = {
    "active": ("pending", "approved"),
    "approved": ("approved",),
    "pending": ("pending",),
    "rejected": ("rejected",),
    "all": ("pending", "approved", "rejected"),
}

FILTER_KEYS = ["date_from", "date_to", "user_id", "custody_id", "category_id", "department_id",
               "vendor_id", "status"]

PRESETS = [
    ("this_month", "هذا الشهر"),
    ("last_month", "الشهر الماضي"),
    ("last_30", "آخر 30 يوم"),
    ("this_year", "هذه السنة"),
]


def preset_range(name: str, today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    if name == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if name == "last_month":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat()
    if name == "last_30":
        return (today - timedelta(days=29)).isoformat(), today.isoformat()
    if name == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    return "", ""


def clean_filters(raw: dict) -> dict:
    f = {k: (str(raw.get(k) or "")).strip() for k in FILTER_KEYS}
    if raw.get("preset"):
        f["date_from"], f["date_to"] = preset_range(raw["preset"])
    if f["status"] not in STATUS_SETS:
        f["status"] = "active"
    for k in ("user_id", "custody_id", "category_id", "department_id", "vendor_id"):
        if not f[k].isdigit():
            f[k] = ""
    return f


def where_clause(f: dict) -> tuple[str, list]:
    statuses = STATUS_SETS[f["status"]]
    parts = [f"i.status IN ({','.join('?' * len(statuses))})"]
    params: list = list(statuses)
    if f["date_from"]:
        parts.append(f"{INV_DATE} >= ?")
        params.append(f["date_from"])
    if f["date_to"]:
        parts.append(f"{INV_DATE} <= ?")
        params.append(f["date_to"])
    for key, col in (("user_id", "i.user_id"), ("custody_id", "i.custody_id"),
                     ("category_id", "i.category_id"), ("department_id", "i.department_id"),
                     ("vendor_id", "i.vendor_id")):
        if f[key]:
            parts.append(f"{col}=?")
            params.append(int(f[key]))
    return " AND ".join(parts), params


BASE_FROM = """FROM invoices i
    JOIN users u ON u.id=i.user_id
    JOIN custodies c ON c.id=i.custody_id
    LEFT JOIN vendors v ON v.id=i.vendor_id
    LEFT JOIN categories cat ON cat.id=i.category_id
    LEFT JOIN departments d ON d.id=i.department_id"""


def _group(db, f: dict, key_sql: str, label_sql: str, order: str = "total DESC", limit: int = 50):
    where, params = where_clause(f)
    return [dict(r) for r in db.execute(
        f"""SELECT {key_sql} AS k, {label_sql} AS label, COUNT(*) AS n,
                   COALESCE(SUM(i.total),0) AS total, COALESCE(SUM(i.vat_amount),0) AS vat,
                   AVG(i.total) AS avg
            {BASE_FROM} WHERE {where} GROUP BY {key_sql} ORDER BY {order} LIMIT {limit}""",
        params).fetchall()]


def _with_share(rows: list[dict], total: float) -> list[dict]:
    top = max((r["total"] for r in rows), default=0) or 1
    for r in rows:
        r["share"] = (r["total"] / total * 100) if total else 0
        r["bar"] = r["total"] / top * 100
    return rows


def summary(db, f: dict) -> dict:
    where, params = where_clause(f)
    k = dict(db.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(i.total),0) AS total, COALESCE(SUM(i.vat_amount),0) AS vat,
                   COALESCE(SUM(i.subtotal),0) AS subtotal, AVG(i.total) AS avg, MAX(i.total) AS max,
                   SUM(i.over_limit) AS over_limit, COUNT(DISTINCT i.vendor_id) AS vendors,
                   COUNT(DISTINCT i.user_id) AS employees
            {BASE_FROM} WHERE {where}""", params).fetchone())
    total = k["total"] or 0
    by_month = _group(db, f, f"substr({INV_DATE},1,7)", f"substr({INV_DATE},1,7)", order="k", limit=24)
    _with_share(by_month, total)
    # مقارنة بالفترة اللي قبلها بنفس الطول
    prev = None
    if f["date_from"] and f["date_to"]:
        try:
            a, b = date.fromisoformat(f["date_from"]), date.fromisoformat(f["date_to"])
            span = (b - a).days + 1
            pf = dict(f, date_from=(a - timedelta(days=span)).isoformat(),
                      date_to=(a - timedelta(days=1)).isoformat())
            pw, pp = where_clause(pf)
            prev_total = db.execute(f"SELECT COALESCE(SUM(i.total),0) {BASE_FROM} WHERE {pw}", pp).fetchone()[0]
            prev = {"total": prev_total, "from": pf["date_from"], "to": pf["date_to"],
                    "change": ((total - prev_total) / prev_total * 100) if prev_total else None}
        except ValueError:
            prev = None
    return {
        "kpi": k, "prev": prev, "by_month": by_month,
        "by_category": _with_share(_group(db, f, "i.category_id", "COALESCE(cat.name,'بدون تصنيف')"), total),
        "by_department": _with_share(_group(db, f, "i.department_id", "COALESCE(d.name,'بدون قسم')"), total),
        "by_employee": _with_share(_group(db, f, "i.user_id", "u.full_name"), total),
        "by_vendor": _with_share(_group(db, f, "i.vendor_id", "COALESCE(v.name,'بدون مورد')", limit=15), total),
    }


def custody_report(db, f: dict) -> list[dict]:
    """لكل عهدة: الأسابيع اللي في الفترة، المبلغ، المصروف، المتبقي، والمرفوض والفوق الحد."""
    wparts, wparams = ["1=1"], []
    if f["date_from"]:
        wparts.append("w.end_date >= ?")
        wparams.append(f["date_from"])
    if f["date_to"]:
        wparts.append("w.start_date <= ?")
        wparams.append(f["date_to"])
    for key, col in (("user_id", "c.user_id"), ("custody_id", "c.id"), ("department_id", "c.department_id")):
        if f[key]:
            wparts.append(f"{col}=?")
            wparams.append(int(f[key]))
    rows = db.execute(
        f"""SELECT c.id, c.name, u.full_name, d.name AS department, c.weekly_amount, c.per_invoice_limit,
                   COUNT(w.id) AS weeks, COALESCE(SUM(w.opening_amount),0) AS opening,
                   SUM(CASE WHEN w.status='open' THEN 1 ELSE 0 END) AS open_weeks,
                   GROUP_CONCAT(w.id) AS week_ids
            FROM custodies c JOIN users u ON u.id=c.user_id
            LEFT JOIN departments d ON d.id=c.department_id
            LEFT JOIN weeks w ON w.custody_id=c.id
            WHERE {' AND '.join(wparts)}
            GROUP BY c.id ORDER BY u.full_name""", wparams).fetchall()
    out = []
    for r in rows:
        ids = [int(x) for x in (r["week_ids"] or "").split(",") if x]
        stats = {"spent": 0, "n": 0, "rejected": 0, "pending": 0, "over": 0, "unlinked": 0}
        if ids:
            marks = ",".join("?" * len(ids))
            s = db.execute(
                f"""SELECT COALESCE(SUM(CASE WHEN status IN ('pending','approved') THEN total END),0) AS spent,
                           SUM(CASE WHEN status IN ('pending','approved') THEN 1 ELSE 0 END) AS n,
                           SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END) AS rejected,
                           SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                           SUM(CASE WHEN over_limit=1 AND status!='rejected' THEN 1 ELSE 0 END) AS over
                    FROM invoices WHERE week_id IN ({marks})""", ids).fetchone()
            stats.update({k: s[k] or 0 for k in ("spent", "n", "rejected", "pending", "over")})
            stats["unlinked"] = db.execute(
                f"""SELECT COUNT(*) FROM invoices i WHERE i.week_id IN ({marks})
                      AND i.status IN ('pending','approved')
                      AND EXISTS (SELECT 1 FROM statement_lines s WHERE s.week_id=i.week_id)
                      AND NOT EXISTS (SELECT 1 FROM statement_lines s WHERE s.invoice_id=i.id)""",
                ids).fetchone()[0]
        item = dict(r)
        item.update(stats)
        item["remaining"] = item["opening"] - stats["spent"]
        item["pct"] = (stats["spent"] / item["opening"] * 100) if item["opening"] else 0
        out.append(item)
    return out


def cheapest_report(db, f: dict) -> dict:
    """لكل تصنيف: الموردين مترتبين بمتوسط الفاتورة، والتوفير لو كله اتشرى من الأرخص.
    ولكل صنف (لو الأصناف متسجلة): أرخص سعر وحدة عند مين."""
    where, params = where_clause(f)
    rows = db.execute(
        f"""SELECT COALESCE(cat.name,'بدون تصنيف') AS category, v.id AS vendor_id, v.name AS vendor,
                   COUNT(*) AS n, SUM(i.total) AS total, AVG(i.total) AS avg, MIN(i.total) AS min,
                   MAX({INV_DATE}) AS last_date
            {BASE_FROM} WHERE {where} AND i.vendor_id IS NOT NULL
            GROUP BY cat.name, v.id ORDER BY cat.name, avg""", params).fetchall()
    cats: dict[str, dict] = {}
    for r in rows:
        c = cats.setdefault(r["category"], {"name": r["category"], "vendors": [], "total": 0, "n": 0})
        c["vendors"].append(dict(r))
        c["total"] += r["total"] or 0
        c["n"] += r["n"]
    for c in cats.values():
        best = c["vendors"][0]["avg"] if c["vendors"] else 0
        c["cheapest"] = c["vendors"][0] if c["vendors"] else None
        c["saving"] = sum(max(0, (v["avg"] - best)) * v["n"] for v in c["vendors"]) if len(c["vendors"]) > 1 else 0
    categories = sorted(cats.values(), key=lambda c: -c["saving"])

    item_rows = db.execute(
        f"""SELECT LOWER(TRIM(it.description)) AS item_key, it.description, v.name AS vendor,
                   it.unit_price, {INV_DATE} AS d
            FROM invoice_items it JOIN invoices i ON i.id=it.invoice_id
            JOIN users u ON u.id=i.user_id JOIN custodies c ON c.id=i.custody_id
            JOIN vendors v ON v.id=i.vendor_id
            LEFT JOIN categories cat ON cat.id=i.category_id
            LEFT JOIN departments d ON d.id=i.department_id
            WHERE {where} AND it.unit_price > 0""", params).fetchall()
    items: dict[str, dict] = {}
    for r in item_rows:
        e = items.setdefault(r["item_key"], {"name": r["description"], "vendors": {}})
        e["vendors"].setdefault(r["vendor"], []).append(r["unit_price"])
    item_list = []
    for e in items.values():
        vs = sorted(({"vendor": k, "avg": sum(p) / len(p), "min": min(p), "n": len(p)}
                     for k, p in e["vendors"].items()), key=lambda x: x["avg"])
        diff = (1 - vs[0]["avg"] / vs[-1]["avg"]) * 100 if len(vs) > 1 and vs[-1]["avg"] else 0
        item_list.append({"name": e["name"], "vendors": vs, "cheapest": vs[0], "diff_pct": diff})
    item_list.sort(key=lambda x: (-(len(x["vendors"]) > 1), -x["diff_pct"]))
    return {"categories": categories, "items": item_list[:100],
            "total_saving": sum(c["saving"] for c in categories)}


def exceptions_report(db, f: dict) -> dict:
    where, params = where_clause(dict(f, status="all"))
    sel = f"""SELECT i.id, i.invoice_number, {INV_DATE} AS d, i.total, i.status, i.over_limit,
                     u.full_name, v.name AS vendor, v.vat_number, c.per_invoice_limit {BASE_FROM}"""
    over = db.execute(f"{sel} WHERE {where} AND i.over_limit=1 ORDER BY d DESC LIMIT 200", params).fetchall()
    no_vat = db.execute(
        f"""{sel} WHERE {where} AND i.status!='rejected' AND (v.vat_number IS NULL OR v.vat_number='')
              AND i.total >= 100 ORDER BY d DESC LIMIT 200""", params).fetchall()
    rejected = db.execute(f"{sel} WHERE {where} AND i.status='rejected' ORDER BY d DESC LIMIT 200",
                          params).fetchall()
    duplicates = db.execute(
        f"""{sel} WHERE {where} AND i.id IN (SELECT invoice_id FROM alerts WHERE kind='duplicate')
            ORDER BY d DESC LIMIT 200""", params).fetchall()
    sw, sp = ["s.invoice_id IS NULL"], []
    if f["date_from"]:
        sw.append("w.end_date >= ?")
        sp.append(f["date_from"])
    if f["date_to"]:
        sw.append("w.start_date <= ?")
        sp.append(f["date_to"])
    if f["user_id"]:
        sw.append("c.user_id=?")
        sp.append(int(f["user_id"]))
    unmatched_lines = db.execute(
        f"""SELECT s.*, u.full_name, w.start_date, w.id AS week_id FROM statement_lines s
            JOIN weeks w ON w.id=s.week_id JOIN custodies c ON c.id=w.custody_id
            JOIN users u ON u.id=c.user_id WHERE {' AND '.join(sw)}
            ORDER BY w.start_date DESC, s.line_no LIMIT 200""", sp).fetchall()
    return {"over": over, "no_vat": no_vat, "rejected": rejected, "duplicates": duplicates,
            "unmatched_lines": unmatched_lines}


def invoice_rows(db, f: dict, limit: int = 2000):
    where, params = where_clause(f)
    return db.execute(
        f"""SELECT i.*, {INV_DATE} AS d, u.full_name, c.name AS custody_name, v.name AS vendor_name,
                   v.vat_number, cat.name AS category_name, d.name AS department_name
            {BASE_FROM} WHERE {where} ORDER BY d DESC, i.id DESC LIMIT {limit}""", params).fetchall()
