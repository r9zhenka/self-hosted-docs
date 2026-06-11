# Offline Docs Mirror

Dump a JS-rendered documentation site to a static folder on an
internet-connected machine, ship the folder through your internal artifact
store, and browse it on a locked-down workstation at `http://localhost`.

Works on client-side-rendered docs (LangChain, Qdrant, Docusaurus, Mintlify,
Read the Docs, etc.) because it renders each page with a real headless browser
before saving.

## 1. On an internet-connected machine: install

```powershell
pip install -r requirements.txt
playwright install chromium
```

## 2. Dump a site

```powershell
python dump_site.py https://qdrant.tech/documentation --out dump/qdrant --max-pages 800
python dump_site.py https://python.langchain.com/docs --out dump/langchain --max-pages 1500
```

Useful flags:

| Flag | Meaning |
|------|---------|
| `--out DIR` | Output folder (default `dump/<host>`) |
| `--max-pages N` | Crawl cap (safety against infinite link space) |
| `--include-subdomains` | Follow subdomains of the same registrable domain |
| `--delay SEC` | Extra settle time after load for heavy JS (default 0.8) |
| `--wait-until` | `networkidle` (default) / `load` / `domcontentloaded` |

The crawler stays on the start domain, downloads CSS/JS/images/fonts, and
rewrites all internal links and assets to **relative paths**, so the dump is
fully self-contained.

## 3. Move it through your artifact store

Zip the output folder and push it to Nexus / S3 / the network share, then pull
it down on the workstation. No tool install is required on the workstation for
serving (step 4 is stdlib-only).

```powershell
Compress-Archive -Path dump/qdrant -DestinationPath qdrant-docs.zip
```

## 4. On the locked-down workstation: serve offline

```powershell
python serve.py dump/qdrant
# -> http://127.0.0.1:8000/
```

`serve.py` uses only the Python standard library and binds to localhost only.

## Memory / disk notes

- The crawler streams pages to **disk** as it goes — RAM use stays flat
  regardless of site size. Your real limit is disk space (usually tens to a few
  hundred MB per docs site).
- The headless Chromium itself uses normal browser memory (~hundreds of MB)
  while crawling; that's on the internet machine, not the workstation.

## Limitations

- Runtime search boxes that call a live API (Algolia, etc.) won't work offline.
  In-page content and navigation do. Use Ctrl+F or your editor to search the
  dumped files instead.
- Pages requiring login/cookies aren't handled by default.
- Always confirm the target site's terms allow mirroring before crawling.
