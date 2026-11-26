
"""
Zealy Link Finder

Searches X through TwitterAPI.io, extracts Zealy campaign links,
removes duplicates, and sends new campaigns to Telegram.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
SEARCH_QUERY = '"zealy.io/cw/" -filter:replies'
STATE_FILE = Path("state.json")

ZEALY_PATTERN = re.compile(
    r"https?://(?:www\.)?zealy\.io/cw/[^\s<>\"]+",
    re.IGNORECASE,
)


def get_secret(name: str) -> str:
    value = os.environ.get(name)

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    return value


def normalize_zealy_url(url: str) -> str | None:
    url = url.strip()
    url = url.rstrip(".,;:!?)]}>\"'")

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None

    if parsed.scheme.lower() != "https":
        return None

    if parsed.netloc.lower() not in {
        "zealy.io",
        "www.zealy.io",
    }:
        return None

    if not parsed.path.lower().startswith("/cw/"):
        return None

    if parsed.path.lower() == "/cw":
        return None

    return urllib.parse.urlunsplit(
        (
            "https",
            "zealy.io",
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def extract_zealy_links(tweet: dict) -> list[str]:
    """
    Extract Zealy campaign URLs from:
    - expanded X URLs
    - tweet text
    - X card URLs
    """

    candidates: list[str] = []

    text = tweet.get("text", "")
    candidates.extend(ZEALY_PATTERN.findall(text))

    entities = tweet.get("entities") or {}

    for item in entities.get("urls", []):
        expanded = item.get("expanded_url")

        if expanded:
            candidates.append(expanded)

    card = tweet.get("card") or {}

    for binding in card.get("binding_values", []):
        value = binding.get("value") or {}

        string_value = value.get("string_value")

        if string_value:
            candidates.append(string_value)

        if binding.get("key") in {
            "card_url",
            "url",
        }:
            candidates.append(
                value.get("string_value", "")
            )

    found: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        normalized = normalize_zealy_url(candidate)

        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    return found


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "tweet_ids": [],
            "campaign_links": [],
        }

    try:
        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return {
            "tweet_ids": [],
            "campaign_links": [],
        }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def search_x() -> list[dict]:
    api_key = get_secret(
        "TWITTERAPI_IO_API_KEY"
    )

    params = urllib.parse.urlencode(
        {
            "query": SEARCH_QUERY,
            "queryType": "Latest",
        }
    )

    request = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            data = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"TwitterAPI.io error ({error.code}): {body}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Could not connect to TwitterAPI.io: "
            f"{error.reason}"
        ) from error

    if data.get("error"):
        raise RuntimeError(
            f"TwitterAPI.io returned an error: {data}"
        )

    return data.get("tweets", [])


def send_telegram(message: str) -> None:
    bot_token = get_secret(
        "TELEGRAM_BOT_TOKEN"
    )

    chat_id = get_secret(
        "TELEGRAM_CHAT_ID"
    )

    url = (
        f"https://api.telegram.org/"
        f"bot{bot_token}/sendMessage"
    )

    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Telegram error ({error.code}): {body}"
        ) from error

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram returned an error: {result}"
        )


def main() -> None:
    state = load_state()

    tweets = search_x()

    known_tweet_ids = set(
        state.get("tweet_ids", [])
    )

    known_campaign_links = set(
        state.get("campaign_links", [])
    )

    new_campaigns: list[tuple[str, dict]] = []

    for tweet in tweets:
        tweet_id = str(tweet.get("id", ""))

        links = extract_zealy_links(tweet)

        if not links:
            continue

        for link in links:
            if link in known_campaign_links:
                continue

            new_campaigns.append(
                (link, tweet)
            )

            known_campaign_links.add(link)

        if tweet_id:
            known_tweet_ids.add(tweet_id)

    state["tweet_ids"] = list(
        known_tweet_ids
    )[-1000:]

    state["campaign_links"] = list(
        known_campaign_links
    )[-2000:]

    save_state(state)

    print(
        f"X search returned {len(tweets)} tweet(s)."
    )

    print(
        f"New Zealy campaigns: "
        f"{len(new_campaigns)}"
    )

    for link, tweet in new_campaigns:
        username = (
            (tweet.get("author") or {})
            .get("userName", "unknown")
        )

        tweet_url = tweet.get(
            "url",
            "",
        )

        message = (
            "🚨 NEW ZEALY CAMPAIGN\n\n"
            f"🔗 {link}\n\n"
            f"👤 @{username}\n"
            f"🐦 {tweet_url}"
        )

        send_telegram(message)

        print(
            f"Sent to Telegram: {link}"
        )


if __name__ == "__main__":
    main()
        
