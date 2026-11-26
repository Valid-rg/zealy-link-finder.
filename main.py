"""
Zealy Link Finder + Credit Monitor

Searches X through TwitterAPI.io, extracts Zealy campaign links,
removes duplicates, sends new campaigns to Telegram, and monitors
both recharge and bonus credits.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_BASE = "https://api.twitterapi.io"
SEARCH_URL = f"{API_BASE}/twitter/tweet/advanced_search"
BALANCE_URL = f"{API_BASE}/oapi/my/info"

SEARCH_QUERY = '"zealy.io/cw/" -filter:replies'
STATE_FILE = Path("state.json")

LOW_CREDIT_THRESHOLD = 5000

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


def api_request(url: str) -> dict:
    api_key = get_secret("TWITTERAPI_IO_API_KEY")

    request = urllib.request.Request(
        url,
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
            return json.loads(
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
            f"Could not connect to TwitterAPI.io: {error.reason}"
        ) from error


def get_credit_balance() -> dict:
    data = api_request(BALANCE_URL)

    if "recharge_credits" not in data:
        raise RuntimeError(
            f"Balance response missing recharge_credits: {data}"
        )

    recharge = int(data.get("recharge_credits") or 0)
    bonus = int(data.get("total_bonus_credits") or 0)

    return {
        "recharge": recharge,
        "bonus": bonus,
        "total": recharge + bonus,
    }


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
    candidates: list[str] = []

    tweet_text = tweet.get("text") or ""
    candidates.extend(ZEALY_PATTERN.findall(tweet_text))

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
    default_state = {
        "tweet_ids": [],
        "campaign_links": [],
        "credit_usage": {},
        "low_credit_alert_sent": False,
    }

    if not STATE_FILE.exists():
        return default_state

    try:
        saved = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(saved, dict):
            return default_state

        for key, value in default_state.items():
            saved.setdefault(key, value)

        return saved

    except Exception:
        print("Could not read state.json. Starting with fresh state.")
        return default_state


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
    params = urllib.parse.urlencode(
        {
            "query": SEARCH_QUERY,
            "queryType": "Latest",
        }
    )

    data = api_request(
        f"{SEARCH_URL}?{params}"
    )

    if data.get("error"):
        raise RuntimeError(
            f"TwitterAPI.io returned an error: {data}"
        )

    return data.get("tweets", [])


def send_telegram(message: str) -> None:
    bot_token = get_secret("TELEGRAM_BOT_TOKEN")
    chat_id = get_secret("TELEGRAM_CHAT_ID")

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


def record_credit_usage(
    state: dict,
    credits_used: int,
) -> None:
    now = datetime.now(timezone.utc)

    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")

    usage = state.setdefault("credit_usage", {})

    if usage.get("day") != today:
        usage["day"] = today
        usage["day_credits"] = 0
        usage["day_searches"] = 0

    if usage.get("month") != month:
        usage["month"] = month
        usage["month_credits"] = 0
        usage["month_searches"] = 0

    usage["day_credits"] += credits_used
    usage["day_searches"] += 1

    usage["month_credits"] += credits_used
    usage["month_searches"] += 1

    usage["last_run_credits"] = credits_used


def check_low_credit_warning(
    state: dict,
    balance: int,
) -> None:
    if balance > LOW_CREDIT_THRESHOLD:
        state["low_credit_alert_sent"] = False
        return

    if state.get("low_credit_alert_sent"):
        return

    message = (
        "⚠️ TWITTERAPI.IO LOW CREDIT WARNING\n\n"
        f"Total remaining credits: {balance:,}\n"
        f"Warning threshold: {LOW_CREDIT_THRESHOLD:,}\n\n"
        "Your Zealy Link Finder may stop searching "
        "if the balance reaches zero.\n"
        "Please check your TwitterAPI.io dashboard."
    )

    send_telegram(message)

    state["low_credit_alert_sent"] = True
    print("Low-credit Telegram warning sent.")


def print_credit_report(
    state: dict,
    balance: dict | None,
) -> None:
    usage = state.get("credit_usage", {})

    print("\n========== CREDIT REPORT ==========")

    if balance is not None:
        print(
            f"Recharge credits: {balance['recharge']:,}"
        )
        print(
            f"Bonus credits: {balance['bonus']:,}"
        )
        print(
            f"TOTAL AVAILABLE: {balance['total']:,}"
        )
    else:
        print("Balance: unavailable")

    print(
        "Credits used in this run: "
        f"{usage.get('last_run_credits', 'unknown')}"
    )

    print(
        "Credits used today (UTC): "
        f"{usage.get('day_credits', 0):,}"
    )

    print(
        "Searches today (UTC): "
        f"{usage.get('day_searches', 0):,}"
    )

    print(
        "Credits used this month (UTC): "
        f"{usage.get('month_credits', 0):,}"
    )

    print(
        "Searches this month (UTC): "
        f"{usage.get('month_searches', 0):,}"
    )

    print("===================================\n")


def main() -> None:
    state = load_state()

    balance_before = None
    balance_after = None

    # Check both recharge and bonus credits.
    try:
        balance_before = get_credit_balance()

        print(
            f"Recharge balance: {balance_before['recharge']:,}"
        )
        print(
            f"Bonus balance: {balance_before['bonus']:,}"
        )
        print(
            f"Total before search: {balance_before['total']:,}"
        )

        check_low_credit_warning(
            state,
            balance_before["total"],
        )

        save_state(state)

        if balance_before["total"] <= 0:
            print("No credits available. Skipping X search.")
            print_credit_report(state, balance_before)
            return

    except Exception as error:
        print(f"Could not check balance before search: {error}")
        print("Continuing with the campaign search.")

    # Run the existing X campaign search.
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

            new_campaigns.append((link, tweet))
            known_campaign_links.add(link)

        if tweet_id:
            known_tweet_ids.add(tweet_id)

    state["tweet_ids"] = list(
        known_tweet_ids
    )[-1000:]

    state["campaign_links"] = list(
        known_campaign_links
    )[-2000:]

    print(f"X search returned {len(tweets)} tweet(s).")
    print(f"New Zealy campaigns: {len(new_campaigns)}")

    # Preserve the existing Telegram campaign alerts.
    for link, tweet in new_campaigns:
        username = (
            (tweet.get("author") or {})
            .get("userName", "unknown")
        )

        tweet_url = tweet.get("url", "")

        message = (
            "🚨 NEW ZEALY CAMPAIGN\n\n"
            f"🔗 {link}\n\n"
            f"👤 @{username}\n"
            f"🐦 {tweet_url}"
        )

        send_telegram(message)

        print(f"Sent to Telegram: {link}")

    # Measure actual balance change after the search.
    try:
        balance_after = get_credit_balance()

        print(
            f"Total after search: {balance_after['total']:,}"
        )

        if balance_before is not None:
            credits_used = max(
                0,
                balance_before["total"] - balance_after["total"],
            )

            record_credit_usage(
                state,
                credits_used,
            )

            print(
                f"Credits consumed this run: {credits_used:,}"
            )

        check_low_credit_warning(
            state,
            balance_after["total"],
        )

    except Exception as error:
        print(f"Could not complete after-search balance check: {error}")

    save_state(state)

    print_credit_report(
        state,
        balance_after,
    )


if __name__ == "__main__":
    main()
        
