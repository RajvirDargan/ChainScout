"""SMTP sending — replaces Claude's Gmail connector.

Returns the Message-ID on success so the existing `commit --message-id` contract is
unchanged: nothing is ever marked as sent unless a send demonstrably succeeded.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


class MailerError(Exception):
    """Send failed. The caller must not commit."""


def credentials(env: dict) -> tuple[str, str]:
    address = (env.get("GMAIL_ADDRESS") or "").strip()
    password = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    if not address or address.startswith("your-"):
        raise MailerError(
            "GMAIL_ADDRESS missing from job_agent/.env — add it and rerun. "
            "Nothing was sent and nothing was committed.")
    if not password or password.startswith("your-"):
        raise MailerError(
            "GMAIL_APP_PASSWORD missing from job_agent/.env. Generate a 16-character "
            "app password at myaccount.google.com/apppasswords (2FA must be on), paste "
            "it in, and rerun. Nothing was sent and nothing was committed.")
    return address, password


def build_message(sender: str, recipients: list[str], subject: str,
                  text_body: str, html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="job-agent.local")
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send(env: dict, recipients: list[str], subject: str, text_body: str,
         html_body: str, log=print) -> str:
    """Send the digest. Returns the Message-ID, or raises MailerError."""
    if not recipients:
        raise MailerError("no recipients configured")
    sender, password = credentials(env)
    msg = build_message(sender, recipients, subject, text_body, html_body)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(sender, password)
            refused = smtp.send_message(msg)
        if refused:
            raise MailerError(f"server refused some recipients: {refused}")
    except smtplib.SMTPAuthenticationError as exc:
        raise MailerError(
            "Gmail rejected the credentials. An app password is required — a normal "
            f"account password will not work here. ({exc.smtp_code})") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailerError(f"SMTP send failed: {type(exc).__name__}: {exc}") from exc

    log(f"  sent to {', '.join(recipients)}")
    return msg["Message-ID"]
