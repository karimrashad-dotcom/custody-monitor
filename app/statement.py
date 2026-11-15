"""بيان العهدة: قراءة سطوره (من Excel أو صورة) وربط كل سطر بفاتورة عن طريق المبلغ."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from .ocr import find_date, normalize, numbers_in

TOLERANCE = 0.5  # فرق التقريب المسموح بالريال

DESC_HEADERS = ["البيان", "الوصف", "التفاصيل", "بيان", "وصف", "description", "details", "item", "الصنف"]
AMOUNT_HEADERS = ["المبلغ", "القيمة", "مبلغ", "قيمة", "amount", "value", "total", "الإجمالي", "الاجمالي"]
DATE_HEADERS = ["التاريخ", "تاريخ", "date"]
VENDOR_HEADERS = ["المورد", "الجهة", "اسم المحل", "vendor", "supplier"]
INVOICE_HEADERS = ["رقم الفاتورة", "فاتورة", "invoice"]


# ---------------- قراءة الملفات ----------------
def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return normalize(str(v)).strip()


def _to_amount(v) -> float | None:
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    nums = numbers_in(_cell_text(v))
    return round(nums[-1], 2) if nums else None


def _find_col(header: list[str], words: list[str]) -> int | None:
    for i, h in enumerate(header):
        low = h.lower()
        if any(w in low for w in words):
            return i
    return None


def parse_rows(rows: list[list]) -> list[dict]:
    """صفوف جدول (من Excel أو CSV) → سطور بيان. بيدور على صف العناوين الأول."""
    rows = [r for r in rows if any(_cell_text(c) for c in r)]
    header_idx, cols = None, {}
    for idx, row in enumerate(rows[:15]):
        header = [_cell_text(c) for c in row]
        amount_col = _find_col(header, AMOUNT_HEADERS)
        desc_col = _find_col(header, DESC_HEADERS)
        if amount_col is not None and desc_col is not None and amount_col != desc_col:
            header_idx = idx
            cols = {"amount": amount_col, "desc": desc_col,
                    "date": _find_col(header, DATE_HEADERS),
                    "vendor": _find_col(header, VENDOR_HEADERS),
                    "invoice": _find_col(header, INVOICE_HEADERS)}
            break
    lines = []
    if header_idx is None:
        # من غير عناوين: آخر رقم في الصف = المبلغ، وأطول نص = البيان
        for row in rows:
            texts = [_cell_text(c) for c in row]
            amounts = [a for a in (_to_amount(c) for c in row if isinstance(c, (int, float)) or numbers_in(_cell_text(c))) if a]
            words = [t for t in texts if sum(ch.isalpha() for ch in t) >= 3]
            if amounts and words:
                lines.append({"description": max(words, key=len), "amount": amounts[-1],
                              "line_date": find_date(" ".join(texts))})
        return lines
    for row in rows[header_idx + 1:]:
        get = lambda key: row[cols[key]] if cols.get(key) is not None and cols[key] < len(row) else None  # noqa: E731
        amount = _to_amount(get("amount"))
        desc = _cell_text(get("desc"))
        if not amount or amount <= 0:
            continue
        low = desc.lower()
        if any(w in low for w in ["الإجمالي", "الاجمالي", "المجموع", "total"]):
            continue  # صف المجموع في آخر البيان
        extra = [x for x in (_cell_text(get("vendor")), _cell_text(get("invoice"))) if x]
        lines.append({"description": desc or " - ".join(extra) or "بدون وصف", "amount": amount,
                      "line_date": find_date(_cell_text(get("date"))) if get("date") is not None else ""})
    return lines


def parse_excel(data: bytes) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    best: list[dict] = []
    for ws in wb.worksheets:
        lines = parse_rows([list(r) for r in ws.iter_rows(values_only=True)])
        if len(lines) > len(best):
            best = lines
    return best


def parse_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    return parse_rows(list(csv.reader(io.StringIO(text))))


def parse_ocr_text(text: str) -> list[dict]:
    """سطور صورة البيان: كل سطر فيه مبلغ + كلام = سطر بيان (تقريبي، الموظف بيراجع)."""
    lines = []
    for raw in normalize(text).splitlines():
        raw = raw.strip()
        if not raw:
            continue
        low = raw.lower()
        if any(w in low for w in ["الإجمالي", "الاجمالي", "المجموع", "total", "الرصيد", "المتبقي"]):
            continue
        nums = [n for n in numbers_in(raw) if n > 0]
        letters = sum(ch.isalpha() for ch in raw)
        if not nums or letters < 3:
            continue
        line_date = find_date(raw)
        desc = re.sub(r"[\d.,/\-:]+", " ", raw)
        desc = " ".join(desc.split())
        lines.append({"description": desc, "amount": round(nums[-1], 2), "line_date": line_date})
    return lines


# ---------------- الربط ----------------
def _close(a, b) -> bool:
    return a is not None and b is not None and abs(a - b) <= TOLERANCE


def match_week(db, week_id: int) -> dict:
    """يربط سطور البيان غير المربوطة بالفواتير بالمبلغ (قبل الضريبة أو الإجمالي).
    لو أكتر من فاتورة بنفس المبلغ، بيفرق بالتاريخ؛ لو لسه مش واضح بيسيبها للمراجعة."""
    lines = db.execute(
        "SELECT * FROM statement_lines WHERE week_id=? AND invoice_id IS NULL ORDER BY line_no",
        (week_id,),
    ).fetchall()
    used = {r["invoice_id"] for r in db.execute(
        "SELECT invoice_id FROM statement_lines WHERE week_id=? AND invoice_id IS NOT NULL", (week_id,))}
    invoices = db.execute(
        "SELECT * FROM invoices WHERE week_id=? AND status IN ('pending','approved') ORDER BY id",
        (week_id,),
    ).fetchall()
    matched = ambiguous = 0
    for line in lines:
        cands = [i for i in invoices if i["id"] not in used
                 and (_close(i["subtotal"], line["amount"]) or _close(i["total"], line["amount"]))]
        if len(cands) > 1 and line["line_date"]:
            same_day = [i for i in cands if i["invoice_date"] == line["line_date"]]
            if same_day:
                cands = same_day
        if len(cands) == 1:
            inv = cands[0]
            link(db, line["id"], inv["id"], "auto")
            used.add(inv["id"])
            matched += 1
        elif len(cands) > 1:
            db.execute("UPDATE statement_lines SET match_status='ambiguous' WHERE id=?", (line["id"],))
            ambiguous += 1
        else:
            db.execute("UPDATE statement_lines SET match_status='none' WHERE id=?", (line["id"],))
    return {"matched": matched, "ambiguous": ambiguous}


def link(db, line_id: int, invoice_id: int | None, how: str) -> None:
    db.execute("UPDATE statement_lines SET invoice_id=?, match_status=? WHERE id=?",
               (invoice_id, how if invoice_id else "none", line_id))
    if invoice_id:
        line = db.execute("SELECT description FROM statement_lines WHERE id=?", (line_id,)).fetchone()
        # الوصف بييجي من البيان
        db.execute("UPDATE invoices SET description=? WHERE id=?", (line["description"], invoice_id))


def reconciliation(db, week_id: int) -> dict:
    lines = db.execute(
        """SELECT s.*, i.total AS inv_total, i.subtotal AS inv_subtotal, i.invoice_number,
                  i.status AS inv_status, v.name AS vendor_name
           FROM statement_lines s LEFT JOIN invoices i ON i.id=s.invoice_id
           LEFT JOIN vendors v ON v.id=i.vendor_id
           WHERE s.week_id=? ORDER BY s.line_no""",
        (week_id,),
    ).fetchall()
    linked = {ln["invoice_id"] for ln in lines if ln["invoice_id"]}
    invoices = db.execute(
        """SELECT i.*, v.name AS vendor_name FROM invoices i LEFT JOIN vendors v ON v.id=i.vendor_id
           WHERE i.week_id=? AND i.status IN ('pending','approved') ORDER BY i.id""",
        (week_id,),
    ).fetchall()
    unmatched_invoices = [i for i in invoices if i["id"] not in linked]
    unmatched_lines = [ln for ln in lines if not ln["invoice_id"]]
    return {
        "lines": lines,
        "invoices": invoices,
        "unmatched_invoices": unmatched_invoices,
        "unmatched_lines": unmatched_lines,
        "statement_total": sum(ln["amount"] or 0 for ln in lines),
        "matched_count": len(lines) - len(unmatched_lines),
        "ok": bool(lines) and not unmatched_lines and not unmatched_invoices,
    }
