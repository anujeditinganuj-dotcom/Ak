"""
Standalone FapHouse resolver API.

GET /api/faphouse?url=<faphouse.com or faphouse2.com video link>
  -> {
       "status": true,
       "creator": "FapHouse API",
       "data": {
         "Filename": "...",
         "size": null,
         "size_bytes": null,
         "thumbnail": "https://...",
         "speed_link": null,
         "m3u8_link": "https://.../master.m3u8",
         "note": "..."
       }
     }

"size"/"size_bytes"/"speed_link" are honestly null, not guessed: faphouse
serves HLS (m3u8) only, and unlike Diskwala's direct single-file links —
where "size_bytes" and a "speed_link" progressive-download URL are both
real, known-upfront values — an HLS stream has no fixed file size until
it's actually downloaded/muxed, and there is no direct-file equivalent of
"speed_link" to hand back. Faking either would be a made-up number, not a
resolved fact.

EMAIL / PASSWORD env vars (optional) let faphouse_downloader.py log in for
account-gated videos — same as the fbot Telegram bot's setup. Without
them, only guest-accessible videos resolve.
"""

import logging
import os
import re
import secrets
import time
from urllib.parse import urlparse

from flask import Flask, jsonify, request

import faphouse_downloader as faphouse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("faphouse_api")

app = Flask(__name__)

# Replaces the old static API_KEY gate — access now requires actually
# logging in with real faphouse.com/faphouse2.com credentials via
# /api/login (below) first, same login flow faphouse_downloader.py
# itself already uses for account-gated videos, just with per-caller
# credentials instead of this server's own single configured account.
#
# Tokens are kept in memory only (not a database) — simplest thing that
# works for this: no setup needed, and a restart just means callers log
# in again, same as any short-lived session would eventually expire
# anyway. Not meant to survive a restart or scale across multiple
# processes; if this ever needs either of those, swap this dict for a
# real session store (Redis, a DB table) without changing the endpoints
# themselves.
_SESSIONS: dict[str, dict] = {}   # token -> {"email": str, "session": requests.Session, "expires_at": float}
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", 3600 * 12))

CREATOR = "FapHouse API"


def _issue_token(email: str, session) -> str:
    token = secrets.token_urlsafe(32)
    _SESSIONS[token] = {"email": email, "session": session, "expires_at": time.time() + TOKEN_TTL_SECONDS}
    return token


def _session_for_token(token: str):
    """Returns the entry dict for a valid, unexpired token, or None.
    Also lazily evicts the entry if it's expired, so _SESSIONS doesn't
    grow forever with dead tokens nobody ever explicitly logged out of."""
    entry = _SESSIONS.get(token)
    if not entry:
        return None
    if time.time() > entry["expires_at"]:
        _SESSIONS.pop(token, None)
        return None
    return entry


def _get_bearer_token() -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):].strip()
    return request.args.get("token", "").strip()


def _sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean[:150] or "video"


def _slug_filename(video_url: str) -> str:
    path = urlparse(video_url).path.strip("/")
    slug = path.split("/")[-1] if path else "video"
    return _sanitize_filename(slug.replace("-", " ")).replace(" ", "-") or "video"


def _error(message: str, status_code: int = 400):
    return jsonify({"status": False, "creator": CREATOR, "message": message}), status_code


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": True,
        "creator": CREATOR,
        "message": "POST /api/login (email, password) to get a token, then "
                    "GET /api/faphouse?url=<link> with 'Authorization: Bearer <token>'.",
    })


@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or request.form.get("email") or "").strip()
    password = (body.get("password") or request.form.get("password") or "").strip()
    if not email or not password:
        return _error("Missing 'email' or 'password'.")

    try:
        session = faphouse.client.login_with_credentials(email, password)
    except Exception as e:
        logger.error(f"login_with_credentials raised for {email[:5]}...: {e}")
        return _error("Login failed due to an internal error — try again shortly.", 502)

    if not session:
        return _error("Invalid email/password, or faphouse rejected the login.", 401)

    token = _issue_token(email, session)
    return jsonify({
        "status": True,
        "creator": CREATOR,
        "data": {
            "token": token,
            "expires_in": TOKEN_TTL_SECONDS,
            "note": "Send this token as 'Authorization: Bearer <token>' (or ?token=<token>) on /api/faphouse.",
        },
    })


@app.route("/api/faphouse", methods=["GET"])
def resolve_faphouse():
    token = _get_bearer_token()
    if not token or not _session_for_token(token):
        return _error("Missing or expired token — log in first via POST /api/login.", 401)

    video_url = (request.args.get("url") or "").strip()
    if not video_url:
        return _error("Missing ?url= parameter.")

    if not faphouse.is_faphouse_link(video_url):
        return _error("Not a faphouse.com/faphouse2.com video link.")

    try:
        meta = faphouse.get_page_meta(video_url) or {}
    except Exception as e:
        logger.warning(f"get_page_meta failed for {video_url}: {e}")
        meta = {}

    try:
        m3u8_url = faphouse.client.get_m3u8_url(video_url)
    except Exception as e:
        logger.error(f"m3u8 resolve failed for {video_url}: {e}")
        m3u8_url = None

    if not m3u8_url:
        return _error(
            "Couldn't resolve a stream URL for that link — the page may "
            "have changed, or the video may be private/members-only.",
            502,
        )

    title = meta.get("title")
    filename = _sanitize_filename(title) if title else _slug_filename(video_url)
    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"

    return jsonify({
        "status": True,
        "creator": CREATOR,
        "data": {
            "Filename": filename,
            "size": None,
            "size_bytes": None,
            "thumbnail": meta.get("poster_url"),
            "speed_link": None,
            "m3u8_link": m3u8_url,
            "note": (
                "faphouse serves HLS (m3u8) only — no direct progressive-"
                "download URL exists, and file size isn't known until the "
                "stream is actually downloaded/muxed (e.g. via ffmpeg)."
            ),
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # threaded=True: resolving a link does blocking network I/O (session/
    # login, page fetch) — without this, Flask's dev server handles one
    # request at a time and a slow resolve would stall every other
    # request behind it. For real production traffic beyond this dev
    # server, run behind gunicorn with multiple workers instead (see
    # Dockerfile).
    app.run(host="0.0.0.0", port=port, threaded=True)
