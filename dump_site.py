#!/usr/bin/env python3
"""
dump_site.py - Mirror a JS-rendered documentation site to disk for offline use.

Renders each page with a headless browser (Playwright/Chromium) so that
client-side-rendered docs (LangChain, Qdrant, Docusaurus, Mintlify, etc.) are
captured with their real content, not an empty SPA shell. Internal links and
assets are downloaded and rewritten to relative paths so the result can be
served as a plain static folder with no internet access.

Usage:
    python dump_site.py https://docs.example.com
    python dump_site.py https://qdrant.tech/documentation --out dump/qdrant --max-pages 800
    python dump_site.py https://python.langchain.com --include-subdomains

Then serve offline:
    python serve.py dump/qdrant        # -> http://localhost:8000

Requires:
    pip install playwright beautifulsoup4
    playwright install chromium
"""

import argparse
import asyncio
import os
import re
import sys
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, unquote

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install beautifulsoup4")

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("Missing dependency: pip install playwright  (then: playwright install chromium)")

from overlay_clean import strip_overlays


# Extensions we treat as downloadable assets rather than crawlable HTML pages.
ASSET_EXT = {
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm",
    ".json", ".map", ".pdf", ".zip", ".wasm", ".avif",
}
# Extensions that are definitely HTML pages worth crawling.
PAGE_EXT = {"", ".html", ".htm", ".php", ".asp", ".aspx"}


def sanitize(segment: str) -> str:
    """Make a URL segment safe to use as a filename on Windows and POSIX."""
    segment = unquote(segment)
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", segment).strip() or "_"


class Mirror:
    def __init__(self, start_url, out_dir, max_pages, include_subdomains,
                 delay, wait_until, timeout, keep_overlays=False):
        self.start_url = start_url.rstrip("/") if start_url.endswith("/") else start_url
        self.out_dir = os.path.abspath(out_dir)
        self.max_pages = max_pages
        self.include_subdomains = include_subdomains
        self.delay = delay
        self.wait_until = wait_until
        self.timeout = timeout
        self.keep_overlays = keep_overlays

        base = urlparse(start_url)
        self.scheme = base.scheme
        self.base_host = base.netloc.lower()
        self.root_domain = self._registrable(self.base_host)

        self.page_queue = deque()
        self.seen_pages = set()          # normalized page URLs queued/visited
        self.asset_cache = {}            # asset URL -> local relative path (from out_dir)
        self.failed = []

    # ---- domain scoping ---------------------------------------------------
    @staticmethod
    def _registrable(host):
        # Naive eTLD+1; good enough for scoping a crawl to one site.
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def in_scope(self, host):
        host = host.lower()
        if host == self.base_host:
            return True
        if self.include_subdomains and host.endswith("." + self.root_domain):
            return True
        return False

    # ---- URL <-> local path ----------------------------------------------
    def page_path(self, url):
        """Local file path (relative to out_dir) for an HTML page URL."""
        p = urlparse(url)
        path = p.path
        if path in ("", "/"):
            rel = "index.html"
        else:
            segs = [sanitize(s) for s in path.strip("/").split("/")]
            last = segs[-1]
            _, ext = os.path.splitext(last)
            if ext.lower() in PAGE_EXT and ext != "":
                rel = "/".join(segs)
            else:
                rel = "/".join(segs) + "/index.html"
        if p.query:
            # Encode query into the filename so distinct pages don't collide.
            qhash = sanitize(p.query)[:60]
            root, ext = os.path.splitext(rel)
            rel = f"{root}__{qhash}{ext or '.html'}"
        # Subdomain pages live under _sub/<host>/ to avoid collisions.
        if p.netloc.lower() != self.base_host:
            rel = f"_sub/{sanitize(p.netloc)}/{rel}"
        return rel

    def asset_path(self, url):
        p = urlparse(url)
        host = p.netloc.lower()
        segs = [sanitize(s) for s in p.path.strip("/").split("/") if s]
        if not segs:
            segs = ["asset"]
        prefix = "_assets" if host == self.base_host else f"_ext/{sanitize(host)}"
        rel = f"{prefix}/" + "/".join(segs)
        if p.query:
            root, ext = os.path.splitext(rel)
            rel = f"{root}__{sanitize(p.query)[:40]}{ext}"
        return rel

    @staticmethod
    def rellink(from_rel, to_rel, fragment=""):
        rel = os.path.relpath(to_rel, os.path.dirname(from_rel)).replace(os.sep, "/")
        return rel + (("#" + fragment) if fragment else "")

    def is_page_url(self, url):
        p = urlparse(url)
        _, ext = os.path.splitext(p.path)
        return ext.lower() in PAGE_EXT or ext.lower() not in ASSET_EXT

    # ---- IO ---------------------------------------------------------------
    def write_file(self, rel_path, data, mode="wb"):
        full = os.path.join(self.out_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, mode, encoding=None if "b" in mode else "utf-8") as f:
            f.write(data)

    async def download_asset(self, context, url):
        """Fetch a binary asset via the browser context; return its local rel path."""
        clean, _ = urldefrag(url)
        if clean in self.asset_cache:
            return self.asset_cache[clean]
        rel = self.asset_path(clean)
        try:
            resp = await context.request.get(clean, timeout=self.timeout)
            if resp.ok:
                body = await resp.body()
                self.write_file(rel, body, "wb")
                self.asset_cache[clean] = rel
                return rel
        except Exception as e:
            self.failed.append((clean, str(e)[:120]))
        self.asset_cache[clean] = None
        return None

    # ---- main crawl -------------------------------------------------------
    async def run(self):
        os.makedirs(self.out_dir, exist_ok=True)
        self.page_queue.append(self.start_url)
        self.seen_pages.add(urldefrag(self.start_url)[0])

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (offline-docs-mirror) Chrome/120 Safari/537.36",
                ignore_https_errors=True,
            )
            page = await context.new_page()
            count = 0

            while self.page_queue and count < self.max_pages:
                url = self.page_queue.popleft()
                count += 1
                print(f"[{count}/{self.max_pages}] {url}")
                try:
                    await page.goto(url, wait_until=self.wait_until, timeout=self.timeout)
                    # Nudge lazy-loaded content / virtualized nav into the DOM.
                    await page.evaluate(
                        "() => window.scrollTo(0, document.body.scrollHeight)"
                    )
                    await page.wait_for_timeout(int(self.delay * 1000))
                    html = await page.content()
                    final_url = page.url
                except Exception as e:
                    self.failed.append((url, str(e)[:120]))
                    continue

                await self.process_page(context, final_url, html)

            await browser.close()

        self.report()

    async def process_page(self, context, page_url, html):
        soup = BeautifulSoup(html, "html.parser")
        from_rel = self.page_path(page_url)

        # Rewrite <a> links: queue in-scope pages, make hrefs relative.
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = urljoin(page_url, href)
            absu, frag = urldefrag(absu)
            pu = urlparse(absu)
            if pu.scheme not in ("http", "https"):
                continue
            if self.in_scope(pu.netloc) and self.is_page_url(absu):
                if absu not in self.seen_pages and len(self.seen_pages) < self.max_pages:
                    self.seen_pages.add(absu)
                    self.page_queue.append(absu)
                a["href"] = self.rellink(from_rel, self.page_path(absu), frag)
            # else: leave external/asset links as absolute (dead offline, that's fine)

        # Download + rewrite assets.
        await self._rewrite_assets(context, soup, page_url, from_rel)

        # Strip cookie/consent overlays so they don't block the offline page.
        if not self.keep_overlays:
            strip_overlays(soup)

        # Write the rewritten, fully-rendered page as UTF-8 HTML.
        full = os.path.join(self.out_dir, from_rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(str(soup))

    # <link> rels that reference a real downloadable file (not page metadata).
    ASSET_LINK_RELS = {"stylesheet", "icon", "shortcut icon", "mask-icon",
                       "apple-touch-icon", "manifest", "preload", "modulepreload"}

    async def _rewrite_assets(self, context, soup, page_url, from_rel):
        # <link> tags: only download true asset rels; skip canonical/alternate/
        # preconnect/dns-prefetch/prev/next which point at pages or hosts, not files.
        for el in soup.find_all("link", href=True):
            rels = {r.lower() for r in (el.get("rel") or [])}
            if not rels & self.ASSET_LINK_RELS:
                continue
            val = el["href"].strip()
            if not val or val.startswith(("data:", "javascript:")):
                continue
            absu = urljoin(page_url, val)
            if urlparse(absu).scheme not in ("http", "https"):
                continue
            local = await self.download_asset(context, absu)
            if local:
                el["href"] = self.rellink(from_rel, local)

        # (tag, attribute) pairs that always point at downloadable resources.
        targets = [
            ("script", "src"),
            ("img", "src"),
            ("source", "src"),
            ("video", "src"),
            ("audio", "src"),
            ("embed", "src"),
        ]
        for tag, attr in targets:
            for el in soup.find_all(tag, **{attr: True}):
                val = el[attr].strip()
                if not val or val.startswith(("data:", "javascript:")):
                    continue
                absu = urljoin(page_url, val)
                if urlparse(absu).scheme not in ("http", "https"):
                    continue
                local = await self.download_asset(context, absu)
                if local:
                    el[attr] = self.rellink(from_rel, local)

            # Handle srcset (img/source) separately.
        for el in soup.find_all(["img", "source"], srcset=True):
            new_parts = []
            for part in el["srcset"].split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                absu = urljoin(page_url, bits[0])
                if urlparse(absu).scheme in ("http", "https"):
                    local = await self.download_asset(context, absu)
                    if local:
                        bits[0] = self.rellink(from_rel, local)
                new_parts.append(" ".join(bits))
            el["srcset"] = ", ".join(new_parts)

    def report(self):
        pages = len([1 for _ in self.seen_pages])
        print("\n" + "=" * 60)
        print(f"Done. Saved into: {self.out_dir}")
        print(f"Pages crawled (queued): {len(self.seen_pages)}")
        print(f"Assets downloaded: {sum(1 for v in self.asset_cache.values() if v)}")
        if self.failed:
            print(f"Failures: {len(self.failed)} (showing first 10)")
            for u, e in self.failed[:10]:
                print(f"  - {u}  :: {e}")
        print("Serve it with:  python serve.py " + os.path.relpath(self.out_dir))
        print("=" * 60)


def main():
    ap = argparse.ArgumentParser(description="Mirror a JS-rendered docs site for offline use.")
    ap.add_argument("url", help="Start URL, e.g. https://qdrant.tech/documentation")
    ap.add_argument("--out", default=None, help="Output dir (default: dump/<host>)")
    ap.add_argument("--max-pages", type=int, default=500, help="Max pages to crawl")
    ap.add_argument("--include-subdomains", action="store_true",
                    help="Also crawl subdomains of the registrable domain")
    ap.add_argument("--delay", type=float, default=0.8,
                    help="Seconds to wait after load for JS to settle")
    ap.add_argument("--wait-until", default="networkidle",
                    choices=["load", "domcontentloaded", "networkidle", "commit"])
    ap.add_argument("--timeout", type=int, default=45000, help="Per-request timeout (ms)")
    ap.add_argument("--keep-overlays", action="store_true",
                    help="Do not strip cookie/consent overlays from saved pages")
    args = ap.parse_args()

    out = args.out or os.path.join("dump", sanitize(urlparse(args.url).netloc))
    m = Mirror(args.url, out, args.max_pages, args.include_subdomains,
               args.delay, args.wait_until, args.timeout, args.keep_overlays)
    asyncio.run(m.run())


if __name__ == "__main__":
    main()
