"""
Zealy Campaign Link Finder

Core link extraction and deduplication logic.

The production bot will receive Google Alert emails through Gmail,
extract Zealy campaign links, normalize them, and send only new
links to Telegram.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


ZEALY_CAMPAIGN_PATTERN = re.compile(
    r"https?://(?:www\.)?zealy\.io/cw/[^\s<>\"]+",
    re.IGNORECASE,
)


def normalize_zealy_url(url: str) -> str | None:
    """
    Convert a Zealy campaign URL into a consistent form.

    Examples:
        https://zealy.io/cw/example/
        https://www.zealy.io/cw/example?utm_source=google

    Both become:

        https://zealy.io/cw/example
    """

    url = url.strip()

    # Remove common punctuation that may be attached to a URL
    url = url.rstrip(".,;:!?)]}>\"'")

    try:
        parsed = urlsplit(url)
    except ValueError:
        return None

    if parsed.scheme.lower() != "https":
        return None

    if parsed.netloc.lower() not in {"zealy.io", "www.zealy.io"}:
        return None

    path = parsed.path.rstrip("/")

    if not path.lower().startswith("/cw/"):
        return None

    # Prevent an empty campaign slug.
    if path.lower() == "/cw":
        return None

    # Tracking/query parameters are deliberately removed.
    # The campaign URL itself is what matters.
    return urlunsplit(
        (
            "https",
            "zealy.io",
            path,
            "",
            "",
        )
    )


def extract_zealy_links(text: str) -> list[str]:
    """
    Find all Zealy campaign links in a block of text.

    Returns unique normalized URLs while preserving their order.
    """

    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for match in ZEALY_CAMPAIGN_PATTERN.findall(text):
        normalized = normalize_zealy_url(match)

        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)

    return found


def find_new_links(
    links: list[str],
    previously_seen: set[str],
) -> list[str]:
    """
    Return only links that have not previously been processed.
    """

    new_links: list[str] = []

    for link in links:
        normalized = normalize_zealy_url(link)

        if normalized and normalized not in previously_seen:
            previously_seen.add(normalized)
            new_links.append(normalized)

    return new_links


def main() -> None:
    """
    Local smoke test.

    This does not connect to Gmail or Telegram.
    """

    sample_email = """
    New Web3 campaign:
    https://zealy.io/cw/example-project

    Another campaign:
    https://www.zealy.io/cw/another-project/?utm_source=google

    Duplicate:
    https://zealy.io/cw/example-project/
    """

    seen: set[str] = set()

    links = extract_zealy_links(sample_email)
    new_links = find_new_links(links, seen)

    print("Detected Zealy campaigns:")

    for link in new_links:
        print(link)


if __name__ == "__main__":
    main()
