#!/usr/bin/env python3
"""
Tiny static file server for Render — serves dashboard.html only.
The dashboard connects to your VPS bot via a configurable server URL
(stored in browser localStorage, set via the 🔗 SERVER button).
"""
import os
from flask import Flask, send_file

app = Flask(__name__)
PORT = int(os.getenv("PORT", "5000"))

@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"))

@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
