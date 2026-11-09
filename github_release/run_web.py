#!/usr/bin/env python3
"""Run the BidShare Lab web prototype locally.

    python run_web.py
    PORT=8000 HOST=0.0.0.0 python run_web.py

For a public demo, use a production WSGI server and put it behind HTTPS:
    gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 4 "webapp:create_app()"
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ.setdefault("MPLBACKEND", "Agg")
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = os.path.join(PROJECT_ROOT, ".cache", "matplotlib")
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print("BidShare Lab web prototype")
    print("  local:   http://%s:%d" % (host if host != "0.0.0.0" else "127.0.0.1", port))
    print("  reviewer: reviewer / Review2026!")
    app.run(host=host, port=port, debug=debug, use_reloader=debug, threaded=True)
