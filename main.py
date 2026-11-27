"""
Zealy Link Finder + Credit Monitor

Searches recent public X posts for Zealy campaign links,
extracts campaign URLs, alerts Telegram once per campaign,
and records search checkpoints and measured credit changes.
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
from typing import Any


API_BASE = "https://api.twitterapi.io"
SEARCH_URL = f"{API_BASE}/twitter/tweet/advanced_search"
BALANCE_URL = f"{API_BASE}/oapi/my/info"

SEARCH_QUERY = '"zealy.io/cw/" -filter:replies'
STATE_FILE = Path("state.json")

LOW_CREDIT_THRESHOLD = 5000
OVERLAP_SECONDS = 300
FIRST_RUN_LOOKBACK_SECONDS = 7200

# Brand-new TwitterAPI.io accounts are limited to one request per 5 seconds.
API_REQUEST_INTERVAL = 5.2
MAX_API_ATTEMPTS = 5

ZEALY_PATTERN = re.compile(
    r"https?://(?:www\.)?zealy\.io/cw/[^\s<>\"']+",
    re.IGNORECASE,
)


def get_secret(name: str) -> str:
    """Read a required secret from the environment."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )
    return value


def api_request(url: str) -> dict[str, Any]:
    """Make a paced API request with bounded retries for transient failures."""
    last_error: Exception | None = None

    for attempt in range(MAX_API_ATTEMPTS):
        # Keep requests below the free-tier QPS limit.
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
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(
                    response.read().decode("utf-8")
                )

            if not isinstance(payload, dict):
                raise RuntimeError(
                    "TwitterAPI.io returned JSON that was not an object."
                )

            return payload

        except urllib.error.HTTPError as error:
            body = error.read().decode(
                "utf-8",
                errors="replace",
            )
            last_error = error

            retryable = (
                error.code in {408, 425, 429}
                or 500 <= error.code <= 599
            )

            if not retryable or attempt == MAX_API_ATTEMPTS - 1:
                raise RuntimeError(
                    f"TwitterAPI.io error ({error.code}): {body}"
                ) from error

            # TwitterAPI.io normally does not send Retry-After headers.
            # Respect one if it is present; otherwise use exponential backoff.
            retry_after = error.headers.get("Retry-After")
            try:
                wait_time = (
                    max(
                        API_REQUEST_INTERVAL,
                        float(retry_after),
                    )
                    if retry_after
                    else min(
                        60.0,
                        API_REQUEST_INTERVAL * (2 ** attempt),
                    )
                )
            except (ValueError, TypeError):
                wait_time = min(
                    60.0,
                    API_REQUEST_INTERVAL * (2 ** attempt),
                )

            print(
                f"Temporary API error ({error.code}). "
                f"Retrying after {wait_time:.1f} seconds..."
            )
            time.sleep(wait_time)

        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error

            if attempt == MAX_API_ATTEMPTS - 1:
                reason = getattr(error, "reason", str(error))
                raise RuntimeError(
                    f"Could not connect to TwitterAPI.io: {reason}"
                ) from error

            wait_time = min(
                60.0,
                API_REQUEST_INTERVAL * (2 ** attempt),
            )
            print(
                "Temporary connection problem. "
                f"Retrying after {wait_time:.1f} seconds..."
            )
            time.sleep(wait_time)

    raise RuntimeError(
        f"TwitterAPI.io request failed after all retry attempts: {last_error}"
    )


def get_credit_balance() -> dict[str, int]:
    """Return recharge, bonus, and total available credits."""
    data = api_request(BALANCE_URL)

    if "recharge_credits" not in data:
        raise RuntimeError(
            "Balance response is missing recharge_credits. "
            f"Response: {data}"
        )

    try:
        recharge = int(float(data.get("recharge_credits") or 0))
        bonus = int(float(data.get("total_bonus_credits") or 0))
    except (ValueError, TypeError) as error:
        raise RuntimeError(
            f"Balance response contains invalid credit values: {data}"
        ) from error

    return {
        "recharge": recharge,
        "bonus": bonus,
        "total": recharge + bonus,
    }


def normalize_zealy_url(url: str) -> str | None:
    """Validate and normalize a Zealy campaign URL."""
    if not isinstance(url, str):
        return None

    url = url.strip().rstrip(".,;:!?)]}>\"'")

    try:
        parsed = urllib.parse.urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() != "https":
        return None

    if hostname not in {"zealy.io", "www.zealy.io"}:
        return None

    if port not in (None, 443):
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


def get_campaign_key(url: str) -> str | None:
    """Use the campaign slug so path variants count as one campaign."""
    normalized = normalize_zealy_url(url)
    if not normalized:
        return None

    try:
        parsed = urllib.parse.urlsplit(normalized)
    except ValueError:
        return None

    parts = [
        urllib.parse.unquote(part)
        for part in parsed.path.split("/")
        if part
    ]

    if len(parts) < 2 or parts[0].lower() != "cw":
        return None

    campaign_slug = parts[1].strip().casefold()
    return campaign_slug or None


def extract_zealy_links(tweet: dict[str, Any]) -> list[str]:
    """Find Zealy links in tweet text, URL entities, and X cards."""
    candidates: list[str] = []

    tweet_text = tweet.get("text")
    if isinstance(tweet_text, str):
        candidates.extend(ZEALY_PATTERN.findall(tweet_text))

    entities = tweet.get("entities") or {}
    if isinstance(entities, dict):
        entity_urls = entities.get("urls") or []
        if isinstance(entity_urls, list):
            for item in entity_urls:
                if not isinstance(item, dict):
                    continue

                for field in ("expanded_url", "unwound_url"):
                    value = item.get(field)
                    if isinstance(value, str) and value:
                        candidates.append(value)

    card = tweet.get("card") or {}
    if isinstance(card, dict):
        binding_values = card.get("binding_values") or {}

        if isinstance(binding_values, dict):
            bindings = list(binding_values.values())
        elif isinstance(binding_values, list):
            bindings = binding_values
        else:
            bindings = []

        for binding in bindings:
            if not isinstance(binding, dict):
                continue

            value = binding.get("value") or binding
            if not isinstance(value, dict):
                continue

            string_value = value.get("string_value")
            if isinstance(string_value, str) and string_value:
                candidates.append(string_value)

    found: list[str] = []
    seen: set[str] = set()

    for candidate in candidates:
        normalized = normalize_zealy_url(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    return found


def parse_tweet_timestamp(created_at: Any) -> int | None:
    """Parse TwitterAPI.io's classic X timestamp, with ISO fallback."""
    if not isinstance(created_at, str) or not created_at.strip():
        return None

    value = created_at.strip()

    # Current TwitterAPI.io tweet responses commonly use this format:
    # Thu Oct 16 12:34:56 +0000 2026
    try:
        tweet_time = datetime.strptime(
            value,
            "%a %b %d %H:%M:%S %z %Y",
        )
    except ValueError:
        try:
            tweet_time = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            return None

    if tweet_time.tzinfo is None:
        tweet_time = tweet_time.replace(tzinfo=timezone.utc)

    return int(tweet_time.timestamp())


def load_state() -> dict[str, Any]:
    """Load state and migrate campaign keys from older state files."""
    default_state: dict[str, Any] = {
        "tweet_ids": [],
        "campaign_links": [],
        "campaign_keys": [],
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
            print("state.json is not a JSON object. Starting fresh.")
            return default_state

        for key, value in default_state.items():
            saved.setdefault(key, value)

        # Repair malformed or missing collections without discarding
        # other valid saved fields.
        for key in ("tweet_ids", "campaign_links", "campaign_keys"):
            if not isinstance(saved.get(key), list):
                saved[key] = []

        if not isinstance(saved.get("credit_usage"), dict):
            saved["credit_usage"] = {}

        saved["tweet_ids"] = [
            str(item) for item in saved["tweet_ids"] if item is not None
        ]
        saved["campaign_links"] = [
            str(item) for item in saved["campaign_links"] if item
        ]

        campaign_keys: list[str] = []
        campaign_key_set: set[str] = set()

        # Preserve insertion order so the 2,000-item limit retains
        # the most recently detected campaigns.
        for item in saved["campaign_keys"]:
            key = str(item).casefold() if item else ""
            if key and key not in campaign_key_set:
                campaign_key_set.add(key)
                campaign_keys.append(key)

        # Migrate keys from campaigns recorded by older versions.
        for link in saved["campaign_links"]:
            key = get_campaign_key(link)
            if key and key not in campaign_key_set:
                campaign_key_set.add(key)
                campaign_keys.append(key)

        saved["campaign_keys"] = campaign_keys[-2000:]

        checkpoint = saved.get("last_search_timestamp")
        if checkpoint is not None:
            try:
                saved["last_search_timestamp"] = int(checkpoint)
            except (ValueError, TypeError):
                saved["last_search_timestamp"] = None

        saved["low_credit_alert_sent"] = bool(
            saved.get("low_credit_alert_sent")
        )

        return saved

    except (OSError, json.JSONDecodeError) as error:
        print(
            "Could not read state.json. Starting with fresh state: "
            f"{error}"
        )
        return default_state


def save_state(state: dict[str, Any]) -> None:
    """Write state atomically so an interrupted write does not corrupt it."""
    temporary_file = STATE_FILE.with_name(
        f"{STATE_FILE.name}.tmp"
    )

    temporary_file.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(STATE_FILE)


def search_x(since_timestamp: int) -> tuple[list[dict[str, Any]], bool]:
    """Search all available pages; report whether pagination completed."""
    query = (
        f"{SEARCH_QUERY} "
        f"since_time:{since_timestamp}"
    )

    tweets: list[dict[str, Any]] = []
    cursor = ""
    pagination_complete = True
    page_number = 0

    print(f"Twitter search query: {query}")

    while True:
        page_number += 1

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

        if data.get("error") or data.get("status") == "error":
            raise RuntimeError(
                "TwitterAPI.io search returned an error: "
                f"{data}"
            )

        page_tweets = data.get("tweets") or []
        if not isinstance(page_tweets, list):
            raise RuntimeError(
                "TwitterAPI.io search response has an invalid tweets field. "
                f"Response: {data}"
            )

        tweets.extend(
            tweet for tweet in page_tweets
            if isinstance(tweet, dict)
        )

        print(
            f"Search page {page_number} returned "
            f"{len(page_tweets)} tweet(s)."
        )

        # A short page does not necessarily mean pagination is over.
        # Continue only when the API provides a usable next cursor.
        if not page_tweets or not data.get("has_next_page"):
            break

        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            print(
                "Pagination stopped because the API did not provide "
                "a new cursor. The checkpoint will not advance."
            )
            pagination_complete = False
            break

        cursor = str(next_cursor)

    return tweets, pagination_complete


def send_telegram(message: str) -> None:
    """Send one Telegram message and verify Telegram accepted it."""
    bot_token = get_secret("TELEGRAM_BOT_TOKEN")
    chat_id = get_secret("TELEGRAM_CHAT_ID")

    url = (
        "https://api.telegram.org/"
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
        with urllib.request.urlopen(request, timeout=30) as response:
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

    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(
            f"Telegram returned an error: {result}"
        )


def record_credit_usage(
    state: dict[str, Any],
    measured_decrease: int,
) -> None:
    """Record the observed before/after balance decrease."""
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

    # Coerce older saved values safely in case a prior state file
    # contains strings or is missing one of these counters.
    for field in (
        "day_credits",
        "day_searches",
        "month_credits",
        "month_searches",
    ):
        try:
            usage[field] = int(usage.get(field, 0) or 0)
        except (ValueError, TypeError):
            usage[field] = 0

    usage["day_credits"] += measured_decrease
    usage["day_searches"] += 1
    usage["month_credits"] += measured_decrease
    usage["month_searches"] += 1
    usage["last_run_credits"] = measured_decrease


def check_low_credit_warning(
    state: dict[str, Any],
    balance: int,
) -> None:
    """Send one warning when credits reach the configured threshold."""
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
    state: dict[str, Any],
    balance: dict[str, int] | None,
    measured_decrease_this_run: int | None = None,
) -> None:
    """Print the current balance and measured usage totals."""
    usage = state.get("credit_usage", {})

    print("\n========== CREDIT REPORT ==========")

    if balance is not None:
        print(f"Recharge credits: {balance['recharge']:,}")
        print(f"Bonus credits: {balance['bonus']:,}")
        print(f"TOTAL AVAILABLE: {balance['total']:,}")
    else:
        print("Balance: unavailable")

    current_run_value = (
        f"{measured_decrease_this_run:,}"
        if measured_decrease_this_run is not None
        else "unavailable"
    )
    print(
        "Measured balance decrease this run: "
        f"{current_run_value}"
    )
    print(
        "Measured balance decrease today (UTC): "
        f"{usage.get('day_credits', 0):,}"
    )
    print(
        "Runs measured today (UTC): "
        f"{usage.get('day_searches', 0):,}"
    )
    print(
        "Measured balance decrease this month (UTC): "
        f"{usage.get('month_credits', 0):,}"
    )
    print(
        "Runs measured this month (UTC): "
        f"{usage.get('month_searches', 0):,}"
    )

    print("===================================\n")


def print_tweet_diagnostics(
    tweets: list[dict[str, Any]],
    known_campaign_keys: set[str],
) -> None:
    """Log returned tweets and extracted links for missed-alert debugging."""
    print("\n========== SEARCH RESULTS ==========")

    if not tweets:
        print("No tweets were returned by TwitterAPI.io.")
        print("===================================\n")
        return

    for index, tweet in enumerate(tweets, start=1):
        tweet_id = str(tweet.get("id") or "unknown")
        author = tweet.get("author") or {}

        if isinstance(author, dict):
            username = (
                author.get("userName")
                or author.get("username")
                or "unknown"
            )
        else:
            username = "unknown"

        created_at = tweet.get("createdAt") or "unknown"
        tweet_text = tweet.get("text")
        text_preview = (
            tweet_text.replace("\n", " ")[:500]
            if isinstance(tweet_text, str)
            else ""
        )

        links = extract_zealy_links(tweet)

        print(f"\nTweet #{index}")
        print(f"Username: @{username}")
        print(f"Tweet ID: {tweet_id}")
        print(f"Created: {created_at}")
        print(f"Text: {text_preview}")

        if not links:
            print("Zealy links found: NONE")
            continue

        for link in links:
            campaign_key = get_campaign_key(link)
            already_known = campaign_key in known_campaign_keys

            print(f"Zealy link: {link}")
            print(f"Campaign key: {campaign_key}")
            print(f"Already known: {already_known}")


def main() -> None:
    """Run one complete Zealy monitoring cycle."""
    state = load_state()

    search_started = int(datetime.now(timezone.utc).timestamp())
    previous_checkpoint = state.get("last_search_timestamp")

    if previous_checkpoint is None:
        since_timestamp = (
            search_started - FIRST_RUN_LOOKBACK_SECONDS
        )
        print(
            "No previous checkpoint found. "
            f"Searching the last {FIRST_RUN_LOOKBACK_SECONDS // 3600} hours."
        )
    else:
        since_timestamp = (
            int(previous_checkpoint) - OVERLAP_SECONDS
        )
        print(
            f"Previous checkpoint: {previous_checkpoint}"
        )
        print(
            f"Searching from checkpoint minus "
            f"{OVERLAP_SECONDS} seconds: {since_timestamp}"
        )

    known_tweet_ids = {
        str(tweet_id)
        for tweet_id in state.get("tweet_ids", [])
    }

    known_campaign_keys = {
        str(key).casefold()
        for key in state.get("campaign_keys", [])
        if key
    }

    print("\n========== ZEALY MONITOR ==========")
    print(f"Search started: {search_started}")
    print(f"Known tweets: {len(known_tweet_ids):,}")
    print(f"Known campaigns: {len(known_campaign_keys):,}")

    balance_before: dict[str, int] | None = None

    try:
        balance_before = get_credit_balance()
        print(
            f"Credits before search: "
            f"{balance_before['total']:,}"
        )
    except Exception as error:
        print(
            "Could not read credit balance before search: "
            f"{error}"
        )

    tweets, pagination_complete = search_x(since_timestamp)

    print(f"\nTotal tweets returned: {len(tweets):,}")
    print(
        f"Pagination complete: "
        f"{'YES' if pagination_complete else 'NO'}"
    )

    print_tweet_diagnostics(
        tweets,
        known_campaign_keys,
    )

    new_campaigns = 0
    skipped_old_tweets = 0
    telegram_failures = 0

    newest_tweet_timestamp: int | None = None

    for tweet in tweets:
        tweet_id = str(tweet.get("id") or "")

        if tweet_id and tweet_id in known_tweet_ids:
            skipped_old_tweets += 1
            continue

        created_timestamp = parse_tweet_timestamp(
            tweet.get("createdAt")
        )

        if (
            created_timestamp is not None
            and (
                newest_tweet_timestamp is None
                or created_timestamp > newest_tweet_timestamp
            )
        ):
            newest_tweet_timestamp = created_timestamp

        links = extract_zealy_links(tweet)

        if tweet_id:
            known_tweet_ids.add(tweet_id)

        if not links:
            continue

        author = tweet.get("author") or {}

        if isinstance(author, dict):
            username = (
                author.get("userName")
                or author.get("username")
                or "unknown"
            )
        else:
            username = "unknown"

        tweet_url = (
            f"https://x.com/{username}/status/{tweet_id}"
            if tweet_id and username != "unknown"
            else ""
        )

        for link in links:
            campaign_key = get_campaign_key(link)

            if not campaign_key:
                continue

            if campaign_key in known_campaign_keys:
                print(
                    f"Skipping known campaign: {link}"
                )
                continue

            message = (
                "🚨 NEW ZEALY CAMPAIGN\n\n"
                f"🔗 {link}\n\n"
                f"👤 @{username}"
            )

            if tweet_url:
                message += f"\n🐦 {tweet_url}"

            try:
                send_telegram(message)

                known_campaign_keys.add(campaign_key)

                campaign_links = state.setdefault(
                    "campaign_links",
                    [],
                )

                if link not in campaign_links:
                    campaign_links.append(link)

                new_campaigns += 1

                print(
                    f"Telegram alert sent: {link}"
                )

            except Exception as error:
                telegram_failures += 1
                print(
                    "Telegram alert failed for "
                    f"{link}: {error}"
                )

    state["tweet_ids"] = list(known_tweet_ids)[-5000:]
    state["campaign_keys"] = list(known_campaign_keys)[-2000:]

    if pagination_complete:
        if newest_tweet_timestamp is not None:
            state["last_search_timestamp"] = (
                newest_tweet_timestamp
            )
            print(
                "Checkpoint advanced to newest returned "
                f"tweet: {newest_tweet_timestamp}"
            )
        elif previous_checkpoint is None:
            state["last_search_timestamp"] = search_started
            print(
                "No tweets were returned on the first run. "
                "Initial checkpoint established."
            )
        else:
            print(
                "No usable new tweet timestamp was found. "
                "Existing checkpoint preserved."
            )
    else:
        print(
            "Pagination was incomplete. "
            "Existing checkpoint preserved."
        )

    balance_after: dict[str, int] | None = None
    measured_decrease = 0

    try:
        balance_after = get_credit_balance()
        print(
            f"Credits after search: "
            f"{balance_after['total']:,}"
        )

        if balance_before is not None:
            measured_decrease = max(
                0,
                balance_before["total"]
                - balance_after["total"],
            )

        record_credit_usage(
            state,
            measured_decrease,
        )

        check_low_credit_warning(
            state,
            balance_after["total"],
        )

    except Exception as error:
        print(
            "Could not complete the after-search "
            f"credit check: {error}"
        )

    save_state(state)

    print("\n========== RUN SUMMARY ==========")
    print(f"Tweets returned: {len(tweets):,}")
    print(f"Old tweets skipped: {skipped_old_tweets:,}")
    print(f"New Zealy campaigns: {new_campaigns:,}")
    print(f"Telegram failures: {telegram_failures:,}")
    print(
        f"Measured credits used: "
        f"{measured_decrease:,}"
    )
    print("State saved successfully.")
    print("=================================\n")


if __name__ == "__main__":
    main()
