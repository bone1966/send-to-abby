"""Abby-Normal upload portal — text a one-time link, drop a file from your phone.

Flow: a trusted caller (Abby, or Keith himself) POSTs /api/send-link with a
phone number; that creates a token, texts a link via Twilio, and the link
opens a one-page upload form that takes one photo or PDF. Files land on
local disk under uploads/ and get listed/fetched through an authenticated
API Abby polls when Keith says to check what came in.
"""

import os
import secrets
import sqlite3
import time
from pathlib import Path

from flask import Flask, abort, request, send_from_directory, render_template_string

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "tokens.db"

APP_SECRET = os.environ.get("APP_SHARED_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
KEITH_PHONE = os.environ.get("KEITH_PHONE", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER", "")

TOKEN_TTL_SECONDS = 24 * 60 * 60
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".pdf", ".gif", ".webp"}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tokens ("
        "token TEXT PRIMARY KEY, created_at REAL, used INTEGER DEFAULT 0, "
        "note TEXT, filename TEXT)"
    )
    return conn


def require_secret():
    sent = request.headers.get("X-App-Secret", "")
    if not APP_SECRET or sent != APP_SECRET:
        abort(401)


def _to_e164(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if phone.strip().startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone


def _send_sms(to_number: str, body: str) -> None:
    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(to=_to_e164(to_number), from_=_to_e164(TWILIO_FROM_NUMBER), body=body)


@app.post("/api/send-link")
def send_link():
    require_secret()
    data = request.get_json(force=True) or {}
    to_number = data.get("to") or KEITH_PHONE
    note = data.get("note", "")
    if not to_number:
        return {"error": "no recipient — pass 'to' or set KEITH_PHONE"}, 400
    token = secrets.token_urlsafe(16)
    conn = db()
    conn.execute(
        "INSERT INTO tokens (token, created_at, note) VALUES (?, ?, ?)",
        (token, time.time(), note),
    )
    conn.commit()
    conn.close()
    link = f"{BASE_URL}/u/{token}"
    _send_sms(to_number, f"Abby-Normal: here's your upload link - {link} (expires in 24h, one file)")
    return {"token": token, "link": link}


UPLOAD_PAGE = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send to Abby-Normal</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;background:#0a0a0a;color:#eee;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
  .card{background:#161616;border-radius:16px;padding:32px;max-width:420px;width:90%;
        text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.4)}
  h1{font-size:20px;margin-bottom:8px}
  p{color:#999;font-size:14px;margin-bottom:24px}
  input[type=file]{display:block;width:100%;margin-bottom:20px;color:#eee}
  button{background:#2dd4bf;color:#042;border:none;border-radius:10px;padding:14px 20px;
         font-size:16px;font-weight:600;width:100%;cursor:pointer}
  .done{color:#2dd4bf;font-size:18px}
</style>
</head>
<body>
<div class="card">
{% if done %}
  <p class="done">Got it. Thanks!</p>
{% elif expired %}
  <h1>Link expired</h1>
  <p>Ask Abby for a new one.</p>
{% else %}
  <h1>Send a file to Abby-Normal</h1>
  <p>Photo or PDF, one file.</p>
  <form method="post" enctype="multipart/form-data">
    <input type="file" name="file" accept="image/*,application/pdf" required>
    <button type="submit">Upload</button>
  </form>
{% endif %}
</div>
</body>
</html>
"""


@app.route("/u/<token>", methods=["GET", "POST"])
def upload(token):
    conn = db()
    row = conn.execute(
        "SELECT created_at, used FROM tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        conn.close()
        abort(404)
    created_at, used = row
    expired = bool(used) or (time.time() - created_at > TOKEN_TTL_SECONDS)
    if request.method == "GET":
        conn.close()
        return render_template_string(UPLOAD_PAGE, expired=expired, done=False)
    if expired:
        conn.close()
        return render_template_string(UPLOAD_PAGE, expired=True, done=False)
    f = request.files.get("file")
    if not f or not f.filename:
        conn.close()
        return render_template_string(UPLOAD_PAGE, expired=False, done=False)
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        conn.close()
        abort(400)
    stored_name = f"{token}{ext}"
    f.save(UPLOAD_DIR / stored_name)
    conn.execute(
        "UPDATE tokens SET used = 1, filename = ? WHERE token = ?",
        (stored_name, token),
    )
    conn.commit()
    conn.close()
    return render_template_string(UPLOAD_PAGE, expired=False, done=True)


@app.get("/api/uploads")
def list_uploads():
    require_secret()
    conn = db()
    rows = conn.execute(
        "SELECT token, created_at, note, filename FROM tokens WHERE filename IS NOT NULL"
    ).fetchall()
    conn.close()
    return {
        "uploads": [
            {"token": t, "created_at": c, "note": n, "filename": fn}
            for t, c, n, fn in rows
        ]
    }


@app.get("/api/uploads/<filename>")
def fetch_upload(filename):
    require_secret()
    safe = Path(filename).name
    if not (UPLOAD_DIR / safe).exists():
        abort(404)
    return send_from_directory(UPLOAD_DIR, safe)


@app.get("/healthz")
def healthz():
    return {"ok": True}
