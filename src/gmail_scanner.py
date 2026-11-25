"""
Gmail scanner for Zealy Campaign Link Finder.

This module connects to Gmail using IMAP, searches for Google Alert
emails, extracts their text, and returns the email contents.

It does NOT send Telegram messages and does NOT store credentials
in the source code.
"""

from __future__ import annotations

import email
import imaplib
import os
from email.header import decode_header
from email.message import Message
from typing import Iterable


GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993


def get_required_environment_variable(name: str) -> str:
    """Get a required environment variable."""

    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable is missing: {name}"
        )

    return value


def decode_email_header(value: str | None) -> str:
    """Safely decode an email header."""

    if not value:
        return ""

    parts = decode_header(value)
    decoded_parts: list[str] = []

    for part, encoding in parts:
        if isinstance(part, bytes):
            decoded_parts.append(
                part.decode(encoding or "utf-8", errors="replace")
            )
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)


def extract_text_from_message(message: Message) -> str:
    """
    Extract readable text from an email message.

    Handles:
    - text/plain
    - text/html
    - multipart messages
    """

    text_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()

            if content_type not in {"text/plain", "text/html"}:
                continue

            if part.get_content_disposition() == "attachment":
                continue

            payload = part.get_payload(decode=True)

            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"

            try:
                text_parts.append(
                    payload.decode(charset, errors="replace")
                )
            except LookupError:
                text_parts.append(
                    payload.decode("utf-8", errors="replace")
                )

    else:
        payload = message.get_payload(decode=True)

        if payload:
            charset = message.get_content_charset() or "utf-8"

            try:
                text_parts.append(
                    payload.decode(charset, errors="replace")
                )
            except LookupError:
                text_parts.append(
                    payload.decode("utf-8", errors="replace")
                )

    return "\n".join(text_parts)


def connect_to_gmail() -> imaplib.IMAP4_SSL:
    """Connect and authenticate to Gmail using environment variables."""

    email_address = get_required_environment_variable(
        "GMAIL_ADDRESS"
    )

    app_password = get_required_environment_variable(
        "GMAIL_APP_PASSWORD"
    )

    mailbox = imaplib.IMAP4_SSL(
        GMAIL_IMAP_HOST,
        GMAIL_IMAP_PORT,
    )

    mailbox.login(
        email_address,
        app_password,
    )

    return mailbox


def fetch_google_alert_emails(
    limit: int = 25,
) -> list[dict[str, str]]:
    """
    Fetch recent Google Alert emails from Gmail.

    Returns a list containing:
    - message_id
    - subject
    - sender
    - date
    - body
    """

    mailbox = connect_to_gmail()

    try:
        status, _ = mailbox.select("INBOX", readonly=True)

        if status != "OK":
            raise RuntimeError("Could not open Gmail inbox.")

        # Google Alerts normally arrives from Google Alerts.
        # We deliberately keep the search broad enough that it can
        # still work if Gmail formats the sender slightly differently.
        status, data = mailbox.search(
            None,
            '(FROM "googlealerts-noreply@google.com")',
        )

        if status != "OK":
            raise RuntimeError("Gmail search failed.")

        message_ids = data[0].split()

        # Only inspect the most recent messages.
        message_ids = message_ids[-limit:]

        results: list[dict[str, str]] = []

        for message_id in reversed(message_ids):
            status, message_data = mailbox.fetch(
                message_id,
                "(RFC822)",
            )

            if status != "OK":
                continue

            raw_email = None

            for item in message_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    raw_email = item[1]
                    break

            if not raw_email:
                continue

            message = email.message_from_bytes(raw_email)

            subject = decode_email_header(
                message.get("Subject")
            )

            sender = decode_email_header(
                message.get("From")
            )

            date = decode_email_header(
                message.get("Date")
            )

            message_id_header = (
                message.get("Message-ID") or ""
            )

            body = extract_text_from_message(message)

            results.append(
                {
                    "message_id": message_id_header,
                    "subject": subject,
                    "sender": sender,
                    "date": date,
                    "body": body,
                }
            )

        return results

    finally:
        try:
            mailbox.logout()
        except Exception:
            pass


def scan_google_alerts() -> Iterable[dict[str, str]]:
    """
    Public helper used by the main application.
    """

    return fetch_google_alert_emails()


if __name__ == "__main__":
    emails = list(scan_google_alerts())

    print(f"Found {len(emails)} Google Alert email(s).")

    for item in emails:
        print("-" * 60)
        print(f"Subject: {item['subject']}")
        print(f"From: {item['sender']}")
        print(f"Date: {item['date']}")
        print(item["body"][:1000])
