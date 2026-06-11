#!/usr/bin/env python3
"""
serve.py - Serve a dumped site folder over localhost with zero internet access.

Usage:
    python serve.py dump/qdrant.tech
    python serve.py dump/qdrant.tech --port 9000

Then open the printed http://localhost:<port> URL in your browser.
Stdlib only - nothing to install, safe for a locked-down workstation.
"""
import argparse
import functools
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler


def main():
    ap = argparse.ArgumentParser(description="Serve an offline site dump on localhost.")
    ap.add_argument("directory", help="Folder produced by dump_site.py")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (default localhost only)")
    args = ap.parse_args()

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        sys.exit(f"Not a directory: {root}")

    handler = functools.partial(SimpleHTTPRequestHandler, directory=root)
    httpd = HTTPServer((args.host, args.port), handler)
    print(f"Serving {root}")
    print(f"  -> http://{args.host}:{args.port}/")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
