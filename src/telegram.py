"""
Telegram integration for Zealy Campaign Link Finder.

Credentials are loaded from environment variables.
They are never stored in this file.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


TELEGRAM_API = "https://api.telegram.org"


def get_required_environment_variable(name: str) -> str:
    """Get a required environment variable."""

    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable is missing: {name}"
        )

    return value


def telegram_request(
    method: str,
    data: dict[str, str],
) -> dict:
    """Send a request to the Telegram Bot API."""

    bot_token = get_required_environment_variable(
        "TELEGRAM_BOT_TOKEN"
    )

    url = f"{TELEGRAM_API}/bot{bot_token}/{method}"

    encoded_data = urllib.parse.urlencode(data).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=encoded_data,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            response_data = response.read().decode("utf-8")

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Telegram API error ({error.code}): {body}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not connect to Telegram: {error.reason}"
        ) from error

    result = json.loads(response_data)

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API returned an error: {result}"
        )

    return result


def send_message(message: str) -> None:
    """Send a text message to the configured Telegram chat."""

    chat_id = get_required_environment_variable(
        "TELEGRAM_CHAT_ID"
    )

    if not message.strip():
        raise ValueError(
            "Telegram message cannot be empty."
        )

    telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "false",
        },
    )


def test_telegram_connection() -> None:
    """Send a simple test message."""

    send_message(
        "✅ Zealy Link Finder connected successfully!\n\n"
        "Telegram connection test passed."
    )


if __name__ == "__main__":
    test_telegram_connection()
