#!/usr/bin/env python3
"""
overlay_clean.py - Remove cookie/consent overlays from dumped HTML.

Offline mirrors freeze consent banners (OneTrust, Cookiebot, CookieYes, generic
GDPR popups) into the static DOM. With no backend to "accept" them, they hover
forever and block clicks/scrolling. This strips those elements and force-enables
scrolling.

Use as a library (imported by dump_site.py) or standalone on an existing dump:

    python overlay_clean.py dump/qdrant
"""
import os
import sys

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4")


# CSS selectors matching common consent/overlay widgets. Case-insensitive (` i`).
CONSENT_SELECTORS = [
    '[id*="onetrust" i]', '[class*="onetrust" i]', '[class*="ot-sdk" i]',
    '[id*="cybotcookiebot" i]',
    '[id*="cookieconsent" i]', '[class*="cookieconsent" i]',
    '[class*="cookie-consent" i]', '[class*="consent-banner" i]',
    '[class*="consent-modal" i]', '[class*="cookie-banner" i]',
    '[class*="cookiebanner" i]', '[id*="cookie-banner" i]',
    '[class*="cky-" i]', '[id*="cky-" i]',
    '[class*="gdpr" i]', '[id*="gdpr" i]',
    '[aria-label*="cookie" i]', '[aria-describedby*="cookie" i]',
]

# Injected to undo scroll-lock that banners leave on <html>/<body>.
_SCROLL_FIX = "html,body{overflow:auto !important;height:auto !important;position:static !important;}"


def strip_overlays(soup: BeautifulSoup) -> int:
    """Remove consent overlays from a parsed page in place. Returns count removed."""
    removed = 0
    for sel in CONSENT_SELECTORS:
        try:
            matches = soup.select(sel)
        except Exception:
            continue
        for el in matches:
            el.decompose()
            removed += 1
    # Force scrolling back on even if a lock class/inline style survived.
    head = soup.head or soup
    style = soup.new_tag("style")
    style.string = _SCROLL_FIX
    head.append(style)
    return removed


def clean_file(path: str) -> int:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    n = strip_overlays(soup)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(soup))
    return n


def clean_dir(root: str) -> None:
    total_files = total_removed = 0
    for dirpath, _, files in os.walk(root):
        for name in files:
            if name.lower().endswith((".html", ".htm")):
                p = os.path.join(dirpath, name)
                total_removed += clean_file(p)
                total_files += 1
    print(f"Cleaned {total_files} HTML files, removed {total_removed} overlay elements.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python overlay_clean.py <dump_dir>")
    target = sys.argv[1]
    if not os.path.isdir(target):
        sys.exit(f"Not a directory: {target}")
    clean_dir(target)
