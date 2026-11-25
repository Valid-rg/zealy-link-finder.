import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

X_BEARER_TOKEN = os.environ["X_BEARER_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

API_URL = "https://api.x.com/2/tweets/search/recent"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
QUERY = "url:zealy.io has:links"
STATE_FILE = Path("state.json")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"baseline_complete": False, "since_id": None}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8"
    )


def x_get(params):
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    last_error = None

    for attempt in range(4):
        try:
            response = requests.get(
                API_URL, headers=headers, params=params, timeout=30
            )
            if response.status_code == 429:
                wait = int(response.headers.get("x-rate-limit-reset", "0")) - int(time.time())
                time.sleep(max(5, min(wait, 60)))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)

    raise RuntimeError(f"X API request failed: {last_error}")


def is_target_zealy_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host in {"zealy.io", "www.zealy.io"} and parsed.path.startswith("/cw/")
    except ValueError:
        return False


def matching_campaign_urls(post):
    found = []
    entities = post.get("entities") or {}
    for item in entities.get("urls", []):
        # expanded_url resolves X's t.co wrapper to the destination.
        candidate = item.get("expanded_url") or item.get("unwound_url") or item.get("url")
        if candidate and is_target_zealy_url(candidate) and candidate not in found:
            found.append(candidate)
    return found


def send_telegram(message):
    response = requests.post(
        TELEGRAM_URL,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram rejected message: {payload}")


def fetch_posts(since_id=None, start_time=None):
    params = {
        "query": QUERY,
        "max_results": 100,
        "tweet.fields": "created_at,author_id,entities",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    if since_id:
        params["since_id"] = since_id
    elif start_time:
        params["start_time"] = start_time

    posts = []
    users = {}

    while True:
        data = x_get(params)
        posts.extend(data.get("data", []))
        for user in data.get("includes", {}).get("users", []):
            users[user["id"]] = user
        next_token = data.get("meta", {}).get("next_token")
        if not next_token:
            break
        params["next_token"] = next_token

    return posts, users


def main():
    state = load_state()

    if not state.get("baseline_complete"):
        # Establish a time boundary on the first run. Existing results are
        # intentionally ignored; if there are none, start_time still ensures
        # that the first later post is not accidentally treated as history.
        from datetime import datetime, timezone
        baseline_time = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        posts, users = fetch_posts()
        if posts:
            state["since_id"] = max(posts, key=lambda p: int(p["id"]))["id"]
        else:
            state["baseline_time"] = baseline_time
        state["baseline_complete"] = True
        save_state(state)
        print(f"Initial baseline established at post ID {state.get('since_id')} or time {state.get('baseline_time')}.")
        return

    posts, users = fetch_posts(
        state.get("since_id"),
        state.get("baseline_time") if not state.get("since_id") else None
    )

    if not posts:
        print("No new X posts to inspect.")
        return

    # Process oldest first so since_id can safely advance after each post.
    posts.sort(key=lambda p: int(p["id"]))

    for post in posts:
        post_id = post["id"]
        username = users.get(post.get("author_id"), {}).get("username", "unknown")
        post_url = f"https://x.com/{username}/status/{post_id}"
        campaign_urls = matching_campaign_urls(post)

        if campaign_urls:
            links = "\n".join(campaign_urls)
            text = post.get("text", "")
            message = (
                "NEW ZEALY CAMPAIGN LINK\n\n"
                f"Found in post by @{username}\n"
                f"Date: {post.get('created_at', 'Unknown')}\n\n"
                f"Post:\n{text}\n\n"
                f"Campaign link(s):\n{links}\n\n"
                f"Original X post:\n{post_url}"
            )
            # Advance state only after Telegram confirms the alert was sent.
            send_telegram(message)

        state["since_id"] = post_id
        save_state(state)

    print(f"Processed {len(posts)} new X posts.")


if __name__ == "__main__":
    main()
