
"""
Zealy Link Finder + Credit Monitor

Searches newer X posts, extracts Zealy campaign links,
removes duplicates, sends Telegram alerts, and tracks credits.
"""

from __future__ import annotations

import json
import os
import re
import time
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
OVERLAP_SECONDS = 60
FIRST_RUN_LOOKBACK_SECONDS = 7200

API_REQUEST_INTERVAL = 5.2
MAX_API_ATTEMPTS = 5

ZEALY_PATTERN = re.compile(
    r"https?://(?:www\.)?zealy\.io/cw/[^\s<>\"']+",
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
    for attempt in range(MAX_API_ATTEMPTS):

        # Stay within the free-tier request limit.
        time.sleep(API_REQUEST_INTERVAL)

        request = urllib.request.Request(
            url,
            headers={
                "X-API-Key": get_secret("TWITTERAPI_IO_API_KEY"),
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

            if error.code == 429 and attempt < MAX_API_ATTEMPTS - 1:
                retry_after = error.headers.get("Retry-After")

                try:
                    wait_time = max(
                        10,
                        int(float(retry_after))
                    ) if retry_after else 10 * (attempt + 1)
                except (ValueError, TypeError):
                    wait_time = 10 * (attempt + 1)

                print(
                    f"Rate limit reached. "
                    f"Waiting {wait_time} seconds before retry..."
                )

                time.sleep(wait_time)
                continue

            raise RuntimeError(
                f"TwitterAPI.io error ({error.code}): {body}"
            ) from error

        except urllib.error.URLError as error:
            raise RuntimeError(
                f"Could not connect to TwitterAPI.io: {error.reason}"
            ) from error

    raise RuntimeError(
        "TwitterAPI.io request failed after all retry attempts."
    )


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
    url = url.strip().rstrip(".,;:!?)]}>\"'")

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
    candidates = []

    candidates.extend(
        ZEALY_PATTERN.findall(tweet.get("text") or "")
    )

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

    found = []
    seen = set()

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
        "last_search_timestamp": None,
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
        print(
            "Could not read state.json. "
            "Starting with fresh state."
        )
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


def search_x(since_timestamp: int) -> list[dict]:
    query = (
        f"{SEARCH_QUERY} "
        f"since_time:{since_timestamp}"
    )

    tweets = []
    cursor = ""

    while True:
        params = {
            "query": query,
            "queryType": "Latest",
        }

        if cursor:
            params["cursor"] = cursor

        url = (
            f"{SEARCH_URL}?"
            f"{urllib.parse.urlencode(params)}"
        )

        data = api_request(url)

        if data.get("error"):
            raise RuntimeError(
                f"TwitterAPI.io returned an error: {data}"
            )

        page_tweets = data.get("tweets") or []
        tweets.extend(page_tweets)

        print(
            f"Search page returned {len(page_tweets)} tweet(s)."
        )

        if not data.get("has_next_page"):
            break

        next_cursor = data.get("next_cursor")

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    return tweets


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

    send_telegram(
        "⚠️ TWITTERAPI.IO LOW CREDIT WARNING\n\n"
        f"Total remaining credits: {balance:,}\n"
        f"Warning threshold: {LOW_CREDIT_THRESHOLD:,}\n\n"
        "Please check your TwitterAPI.io dashboard."
    )

    state["low_credit_alert_sent"] = True
    print("Low-credit warning sent.")


def print_credit_report(
    state: dict,
    balance: dict | None,
) -> None:
    usage = state.get("credit_usage", {})

    print("\n========== CREDIT REPORT ==========")

    if balance is not None:
        print(f"Recharge credits: {balance['recharge']:,}")
        print(f"Bonus credits: {balance['bonus']:,}")
        print(f"TOTAL AVAILABLE: {balance['total']:,}")
    else:
        print("Balance: unavailable")

    print(
        f"Credits used this run: "
        f"{usage.get('last_run_credits', 'unknown')}"
    )
    print(
        f"Credits used today (UTC): "
        f"{usage.get('day_credits', 0):,}"
    )
    print(
        f"Searches today (UTC): "
        f"{usage.get('day_searches', 0):,}"
    )
    print(
        f"Credits used this month (UTC): "
        f"{usage.get('month_credits', 0):,}"
    )
    print(
        f"Searches this month (UTC): "
        f"{usage.get('month_searches', 0):,}"
    )

    print("===================================\n")


def main() -> None:
    state = load_state()

    balance_before = None
    balance_after = None

    search_started = int(time.time())

    previous_search = state.get("last_search_timestamp")

    if previous_search:
        since_timestamp = max(
            0,
            int(previous_search) - OVERLAP_SECONDS,
        )
    else:
        since_timestamp = (
            search_started - FIRST_RUN_LOOKBACK_SECONDS
        )

    print(
        "Searching for posts newer than Unix timestamp: "
        f"{since_timestamp}"
    )

    # Check account balance before searching.
    try:
        balance_before = get_credit_balance()

        print(
            f"Recharge credits: {balance_before['recharge']:,}"
        )
        print(
            f"Bonus credits: {balance_before['bonus']:,}"
        )
        print(
            f"Total available: {balance_before['total']:,}"
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
        print(f"Balance check failed: {error}")
        print("Continuing with X search.")

    # Search only the new time window.
    tweets = search_x(since_timestamp)

    known_tweet_ids = set(
        state.get("tweet_ids", [])
    )

    known_campaign_links = set(
        state.get("campaign_links", [])
    )

    new_campaigns = []

    for tweet in tweets:
        tweet_id = str(tweet.get("id", ""))

        if tweet_id and tweet_id in known_tweet_ids:
            continue

        links = extract_zealy_links(tweet)

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

    print(f"Total tweets returned: {len(tweets)}")
    print(f"New Zealy campaigns: {len(new_campaigns)}")

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

    # Save checkpoint after search and notifications succeed.
    state["last_search_timestamp"] = search_started

    # Check balance again.
    try:
        balance_after = get_credit_balance()

        print(
            f"Total balance after search: "
            f"{balance_after['total']:,}"
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
                f"Measured credits used: {credits_used:,}"
            )

        check_low_credit_warning(
            state,
            balance_after["total"],
        )

    except Exception as error:
        print(f"After-search balance check failed: {error}")

    save_state(state)

    print_credit_report(
        state,
        balance_after,
    )


if __name__ == "__main__":
    main()
    
