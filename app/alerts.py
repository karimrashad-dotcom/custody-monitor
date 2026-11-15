"""التنبيهات: جوه البرنامج دايماً، وبالإيميل لو إعدادات البريد مظبوطة."""
import logging
import smtplib
import threading
from email.message import EmailMessage

from .db import get_setting, week_totals

log = logging.getLogger("custody.alerts")

KIND_LABELS = {
    "over_limit": "تخطي حد الفاتورة",
    "duplicate": "فاتورة مكررة",
    "over_balance": "تخطي رصيد العهدة",
    "statement": "رفع كشف العهدة",
}


def _send_email(host, port, user, password, sender, to, subject, body, use_tls):
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender or user
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP(host, int(port or 587), timeout=20) as smtp:
            if use_tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except Exception:
        log.exception("فشل إرسال إيميل التنبيه")


def create_alert(db, kind: str, message: str, invoice_id=None, week_id=None) -> None:
    db.execute(
        "INSERT INTO alerts(kind,message,invoice_id,week_id) VALUES (?,?,?,?)",
        (kind, message, invoice_id, week_id),
    )
    to = get_setting(db, "alert_email")
    host = get_setting(db, "smtp_host")
    if to and host and kind != "statement":
        args = (
            host,
            get_setting(db, "smtp_port", "587"),
            get_setting(db, "smtp_user"),
            get_setting(db, "smtp_password"),
            get_setting(db, "smtp_from"),
            to,
            f"تنبيه عهد: {KIND_LABELS.get(kind, kind)}",
            message,
            get_setting(db, "smtp_tls", "1") == "1",
        )
        threading.Thread(target=_send_email, args=args, daemon=True).start()


def run_invoice_checks(db, invoice_id: int) -> list[str]:
    """تتنفذ لما الموظف يبعت الفاتورة. بترجع أنواع التنبيهات اللي اتعملت."""
    inv = db.execute(
        """SELECT i.*, c.name AS custody_name, c.per_invoice_limit, c.weekly_amount,
                  u.full_name, v.name AS vendor_name, v.vat_number
           FROM invoices i
           JOIN custodies c ON c.id=i.custody_id
           JOIN users u ON u.id=i.user_id
           LEFT JOIN vendors v ON v.id=i.vendor_id
           WHERE i.id=?""",
        (invoice_id,),
    ).fetchone()
    raised = []
    total = inv["total"] or 0
    who = f"{inv['full_name']} ({inv['custody_name']})"
    vendor = inv["vendor_name"] or "مورد غير معروف"

    over = total > inv["per_invoice_limit"]
    db.execute("UPDATE invoices SET over_limit=? WHERE id=?", (1 if over else 0, invoice_id))
    if over:
        create_alert(
            db, "over_limit",
            f"{who} رفع فاتورة بمبلغ {total:,.2f} ريال من {vendor}، "
            f"والحد المسموح للفاتورة الواحدة {inv['per_invoice_limit']:,.2f} ريال.",
            invoice_id=invoice_id, week_id=inv["week_id"],
        )
        raised.append("over_limit")

    dup = None
    if inv["invoice_number"]:
        dup = db.execute(
            """SELECT id FROM invoices WHERE id<>? AND status<>'draft' AND invoice_number=?
                 AND (vendor_id IS ? OR vendor_id=?)""",
            (invoice_id, inv["invoice_number"], inv["vendor_id"], inv["vendor_id"]),
        ).fetchone()
    if not dup and inv["image_sha256"]:
        dup = db.execute(
            "SELECT id FROM invoices WHERE id<>? AND status<>'draft' AND image_sha256=?",
            (invoice_id, inv["image_sha256"]),
        ).fetchone()
    if dup:
        create_alert(
            db, "duplicate",
            f"{who} رفع فاتورة رقم {inv['invoice_number'] or '-'} من {vendor} "
            f"شكلها مكررة مع فاتورة سابقة رقم #{dup['id']} في النظام.",
            invoice_id=invoice_id, week_id=inv["week_id"],
        )
        raised.append("duplicate")

    totals = week_totals(db, inv["week_id"])
    if totals["remaining"] < 0:
        already = db.execute(
            "SELECT 1 FROM alerts WHERE kind='over_balance' AND week_id=?", (inv["week_id"],)
        ).fetchone()
        if not already:
            create_alert(
                db, "over_balance",
                f"مصروف عهدة {who} الأسبوع ده وصل {totals['spent']:,.2f} ريال "
                f"وعدّى مبلغ العهدة {totals['opening']:,.2f} ريال.",
                invoice_id=invoice_id, week_id=inv["week_id"],
            )
            raised.append("over_balance")
    return raised
