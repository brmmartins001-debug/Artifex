"""E-mails: sempre registrados na tabela outbox; só são enviados se houver SMTP configurado."""
import smtplib
from email.message import EmailMessage

from flask import current_app

from . import db as dbm


def smtp_configured():
    return bool(current_app.config.get("SMTP_HOST") and current_app.config.get("SMTP_FROM"))


def send_mail(to, subject, body):
    """Registra o e-mail. Retorna o status: 'sent', 'failed' ou 'not_sent' (sem provedor)."""
    status = "not_sent"
    if smtp_configured():
        try:
            msg = EmailMessage()
            msg["From"] = current_app.config["SMTP_FROM"]
            msg["To"] = to
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(current_app.config["SMTP_HOST"], current_app.config["SMTP_PORT"], timeout=15) as s:
                s.starttls()
                if current_app.config.get("SMTP_USER"):
                    s.login(current_app.config["SMTP_USER"], current_app.config["SMTP_PASSWORD"])
                s.send_message(msg)
            status = "sent"
        except Exception:  # noqa: BLE001 - qualquer falha de envio vira 'failed'
            current_app.logger.exception("Falha ao enviar e-mail")
            status = "failed"
    dbm.execute(
        "INSERT INTO outbox (to_email, subject, body, status, created_at) VALUES (?,?,?,?,?)",
        (to, subject, body, status, dbm.utcnow()),
    )
    return status
