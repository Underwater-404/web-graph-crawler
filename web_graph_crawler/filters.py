"""Domain filtering for discovered links: drop mainstream "famous" sites.

Dork sweeps are usually after the long tail — small sites, forums, open
directories, exposed files — not the mega-platforms that dominate generic
results. :data:`FAMOUS_DOMAINS` is a curated blocklist of those platforms;
discovery drops any result whose host matches one (unless disabled).
"""

from __future__ import annotations

from urllib.parse import urlparse

#: Mainstream platforms that clutter broad dork results. Registrable domains;
#: matching is suffix-aware so subdomains (m.facebook.com) are caught too.
FAMOUS_DOMAINS: frozenset[str] = frozenset(
    {
        # Search engines / portals
        "google.com", "google.co.uk", "bing.com", "yahoo.com", "duckduckgo.com",
        "baidu.com", "yandex.com", "yandex.ru", "ask.com", "aol.com", "ecosia.org",
        # Google properties
        "youtube.com", "youtu.be", "blogger.com", "googleusercontent.com",
        "googleblog.com", "goo.gl",
        # Social networks
        "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com",
        "t.co", "tiktok.com", "linkedin.com", "reddit.com", "redd.it",
        "pinterest.com", "snapchat.com", "tumblr.com", "quora.com", "medium.com",
        "vk.com", "weibo.com",
        # Messaging
        "whatsapp.com", "telegram.org", "t.me", "discord.com", "discord.gg",
        # Commerce / streaming / big tech
        "amazon.com", "ebay.com", "aliexpress.com", "netflix.com", "spotify.com",
        "apple.com", "microsoft.com", "live.com", "office.com", "adobe.com",
        "paypal.com",
        # Reference / media
        "wikipedia.org", "wikimedia.org", "wiktionary.org", "imdb.com",
        "britannica.com",
        # Infra / misc mega
        "cloudflare.com", "gravatar.com", "w3.org", "mozilla.org", "gstatic.com",
    }
)


def _matches(host: str, domains: frozenset[str] | set[str]) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def is_excluded(
    host: str,
    exclude_domains: frozenset[str] | set[str] = frozenset(),
    exclude_famous: bool = True,
) -> bool:
    """True if ``host`` should be dropped from discovery results."""
    if exclude_domains and _matches(host, exclude_domains):
        return True
    if exclude_famous and _matches(host, FAMOUS_DOMAINS):
        return True
    return False


def dedup_key(url: str) -> str:
    """Aggressive de-duplication key for discovered links.

    Collapses ``http``/``https``, a leading ``www.``, and trailing slashes so
    obvious duplicates of the same page map together. The query string is kept
    because ``?id=1`` and ``?id=2`` are genuinely different pages.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}?{parsed.query}" if parsed.query else f"{host}{path}"


def parse_domain_list(value: str | None) -> frozenset[str]:
    """Parse a comma/space separated ``--exclude-domains`` value into a set."""
    if not value:
        return frozenset()
    parts = value.replace(",", " ").split()
    return frozenset(p.lower().strip().lstrip("*.").strip(".") for p in parts if p.strip())
