"""بيان العهدة: قراءة سطوره (من Excel أو صورة) وربط كل سطر بفاتورة عن طريق المبلغ."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

from .ocr import find_date, normalize, numbers_in

TOLERANCE = 0.5  # فرق التقريب المسموح بالريال

DESC_HEADERS = ["بيان", "البيان", "الوصف", "وصف", "التفاصيل", "description", "details", "item", "الصنف"]
TOTAL_HEADERS = ["اجمال", "إجمال", "الإجمالي", "المجموع", "total", "gross"]
SUBTOTAL_HEADERS = ["قيمة المبلغ", "المبلغ", "قبل الضريبة", "صافي", "مبلغ", "amount", "net", "القيمة", "قيمة"]
VAT_HEADERS = ["ضريبة", "الضريبة", "vat", "tax"]
DATE_HEADERS = ["التاريخ", "تاريخ", "date"]
COST_HEADERS = ["مركز التكلفة", "مركز تكلفة", "cost center", "cost centre"]
VENDOR_HEADERS = ["المورد", "الجهة", "اسم المحل", "vendor", "supplier"]
SKIP_WORDS = ["الإجمالي", "الاجمالي", "اجماليات", "الاجماليات", "المجموع", "total"]


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


def _find_cols(header: list[str], words: list[str], taken: set[int]) -> list[int]:
    return [i for i, h in enumerate(header) if i not in taken and h and any(w in h.lower() for w in words)]


def _map_header(header: list[str]) -> dict | None:
    """يحدد الأعمدة من صف العناوين. الترتيب مهم: (اجمالي القيمة) قبل (القيمة)."""
    taken: set[int] = set()
    cols: dict = {}
    for key, words in (("total", TOTAL_HEADERS), ("vat", VAT_HEADERS), ("date", DATE_HEADERS),
                       ("cost", COST_HEADERS), ("vendor", VENDOR_HEADERS)):
        found = _find_cols(header, words, taken)
        cols[key] = found[0] if found else None
        taken.update(found[:1])
    found = _find_cols(header, SUBTOTAL_HEADERS, taken)
    cols["subtotal"] = found[0] if found else None
    taken.update(found[:1])
    cols["desc"] = _find_cols(header, DESC_HEADERS, taken)  # ممكن أكتر من عمود: بيان 1 + بيان 2
    if not cols["desc"] or (cols["total"] is None and cols["subtotal"] is None):
        return None
    return cols


def _date_order(raw_dates: list[str]) -> str:
    """التواريخ زي 7/13/2026 معناها شهر/يوم. بنشوف الملف كله ونقرر."""
    for d in raw_dates:
        m = re.match(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d{2})", d)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12:
                return "dmy"
            if b > 12:
                return "mdy"
    return "dmy"


def _parse_date(raw: str, order: str) -> str:
    raw = normalize(raw or "")
    m = re.match(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d{2})", raw)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d, mo = (a, b) if order == "dmy" else (b, a)
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return ""
    return find_date(raw)


def parse_rows(rows: list[list]) -> list[dict]:
    """صفوف جدول (Excel أو CSV أو جدول PDF) → سطور بيان. بيدور على صف العناوين الأول."""
    rows = [r for r in rows if any(_cell_text(c) for c in r)]
    header_idx, cols = None, None
    for idx, row in enumerate(rows[:20]):
        cols = _map_header([_cell_text(c) for c in row])
        if cols:
            header_idx = idx
            break
    lines = []
    if header_idx is None:
        # من غير عناوين: آخر رقم في الصف = المبلغ، وأطول نص = البيان
        for row in rows:
            texts = [_cell_text(c) for c in row]
            amounts = [a for a in (_to_amount(c) for c in row if isinstance(c, (int, float)) or numbers_in(_cell_text(c))) if a]
            words = [t for t in texts if sum(ch.isalpha() for ch in t) >= 3]
            if amounts and words and not any(w in " ".join(words) for w in SKIP_WORDS):
                lines.append({"description": max(words, key=len), "amount": amounts[-1],
                              "subtotal": None, "vat": None, "cost_center": "",
                              "line_date": find_date(" ".join(texts))})
        return lines

    def get(row, key):
        i = cols.get(key)
        return row[i] if i is not None and i < len(row) else None

    body = rows[header_idx + 1:]
    order = _date_order([_cell_text(get(r, "date")) for r in body if get(r, "date") is not None])
    for row in body:
        total = _to_amount(get(row, "total"))
        subtotal = _to_amount(get(row, "subtotal"))
        vat = _to_amount(get(row, "vat"))
        if total is None and subtotal is not None:
            total = round(subtotal + (vat or 0), 2)
        desc = " - ".join(t for t in (_cell_text(row[i]) for i in cols["desc"] if i < len(row)) if t)
        if not total or total <= 0:
            continue
        if any(w in desc.lower() for w in SKIP_WORDS):
            continue  # صف المجموع في آخر البيان
        raw_date = get(row, "date")
        line_date = raw_date.date().isoformat() if isinstance(raw_date, datetime) else \
            raw_date.isoformat() if isinstance(raw_date, date) else _parse_date(_cell_text(raw_date), order)
        lines.append({
            "description": desc or _cell_text(get(row, "vendor")) or "بدون وصف",
            "amount": total, "subtotal": subtotal, "vat": vat,
            "cost_center": _cell_text(get(row, "cost")), "line_date": line_date,
        })
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


def parse_pdf(data: bytes) -> list[dict]:
    """PDF محفوظ من Excel: بنقرا الجدول من النص اللي جوه الملف. لو PDF ممسوح (صورة) بيرجع فاضي."""
    from .pdftext import extract_tables, extract_text

    lines = parse_rows(extract_tables(data))
    if lines:
        return lines
    lines = parse_rows(extract_tables(data, by_text=True))  # جدول من غير خطوط
    if lines:
        return lines
    text = extract_text(data)
    return parse_ocr_text(text) if text.strip() else []


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
        lines.append({"description": desc, "amount": round(nums[-1], 2), "subtotal": None, "vat": None,
                      "cost_center": "", "line_date": line_date})
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
        amounts = [a for a in (line["amount"], line["subtotal"]) if a]
        cands = [i for i in invoices if i["id"] not in used
                 and any(_close(i["subtotal"], a) or _close(i["total"], a) for a in amounts)]
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
