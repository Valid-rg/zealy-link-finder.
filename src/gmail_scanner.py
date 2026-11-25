"""
Gmail scanner for Zealy Campaign Link Finder.

Connects to Gmail using IMAP, finds Google Alert emails,
and returns their contents for Zealy link extraction.

Credentials are loaded from GitHub Actions environment variables.
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

GOOGLE_ALERT_SENDER = "googlealerts-noreply@google.com"


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
                part.decode(
                    encoding or "utf-8",
                    errors="replace",
                )
            )
        else:
            decoded_parts.append(part)

    return "".join(decoded_parts)


def decode_payload(payload: bytes, charset: str | None) -> str:
    """Decode an email payload safely."""

    try:
        return payload.decode(
            charset or "utf-8",
            errors="replace",
        )
    except LookupError:
        return payload.decode(
            "utf-8",
            errors="replace",
        )


def extract_text_from_message(message: Message) -> str:
    """
    Extract readable text and HTML from an email.

    Both text/plain and text/html are kept because
    Google Alert emails may contain Zealy links inside HTML.
    """

    text_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()

            if content_type not in {
                "text/plain",
                "text/html",
            }:
                continue

            if part.get_content_disposition() == "attachment":
                continue

            payload = part.get_payload(decode=True)

            if payload is None:
                continue

            text_parts.append(
                decode_payload(
                    payload,
                    part.get_content_charset(),
                )
            )

    else:
        payload = message.get_payload(decode=True)

        if payload:
            text_parts.append(
                decode_payload(
                    payload,
                    message.get_content_charset(),
                )
            )

    return "\n".join(text_parts)


def connect_to_gmail() -> imaplib.IMAP4_SSL:
    """Connect and authenticate to Gmail."""

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


def is_google_alert(
    sender: str,
    subject: str,
) -> bool:
    """
    Check whether an email appears to be a Google Alert.

    We accept either:
    - the normal Google Alerts sender, or
    - a subject containing "Google Alert".
    """

    sender_lower = sender.lower()
    subject_lower = subject.lower()

    return (
        GOOGLE_ALERT_SENDER in sender_lower
        or "google alert" in subject_lower
    )


def fetch_google_alert_emails(
    limit: int = 25,
) -> list[dict[str, str]]:
    """
    Fetch recent Google Alert emails.

    Searches both:
    - Gmail Inbox
    - Gmail All Mail

    It also searches by both sender and subject so that
    small changes in Gmail's email formatting do not break
    the scanner.
    """

    mailbox = connect_to_gmail()

    try:
        candidate_messages: list[
            tuple[str, bytes]
        ] = []

        folders = [
            "INBOX",
            "[Gmail]/All Mail",
        ]

        search_queries = [
            '(FROM "googlealerts-noreply@google.com")',
            '(SUBJECT "Google Alert")',
        ]

        for folder in folders:
            try:
                status, _ = mailbox.select(
                    folder,
                    readonly=True,
                )

                if status != "OK":
                    continue

            except imaplib.IMAP4.error:
                continue

            for query in search_queries:
                try:
                    status, data = mailbox.search(
                        None,
                        query,
                    )

                except imaplib.IMAP4.error:
                    continue

                if status != "OK" or not data:
                   
