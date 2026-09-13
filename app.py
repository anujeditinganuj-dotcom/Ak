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
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", 3600 * 24 * 30))  # default: 30 days

# Auto-login credentials
_AUTO_EMAIL    = os.environ.get("EMAIL", "rockstarga69@gmail.com")
_AUTO_PASSWORD = os.environ.get("PASSWORD", "Jaiisbeast@0")
_AUTO_TOKEN: str | None = None  # set at startup

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
    # Check Authorization header (case-insensitive)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    # Check ?token= query param — strip any URL fragment (#...) that
    # browsers may accidentally include in the query string
    token = request.args.get("token", "").strip()
    if "#" in token:
        token = token[:token.index("#")]
    return token


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


def _auto_login():
    """Login with credentials at startup — token persists for 30 days."""
    global _AUTO_TOKEN
    if not _AUTO_EMAIL or not _AUTO_PASSWORD:
        logger.warning("EMAIL/PASSWORD not set — auto-login skipped")
        return
    try:
        session = faphouse.client.login_with_credentials(_AUTO_EMAIL, _AUTO_PASSWORD)
        if session:
            _AUTO_TOKEN = _issue_token(_AUTO_EMAIL, session)
            logger.info(f"Auto-login OK — token: {_AUTO_TOKEN[:10]}...")
        else:
            logger.error("Auto-login failed — check credentials")
    except Exception as e:
        logger.error(f"Auto-login error: {e}")


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": True,
        "creator": CREATOR,
        "message": "POST /api/login (email, password) to get a token, then "
                    "GET /api/faphouse?url=<link> with 'Authorization: Bearer <token>'.",
    })


@app.route("/api/token", methods=["GET"])
def get_auto_token():
    """Get auto-generated token — no manual login needed."""
    global _AUTO_TOKEN
    if not _AUTO_TOKEN or not _session_for_token(_AUTO_TOKEN):
        _auto_login()
    if _AUTO_TOKEN and _session_for_token(_AUTO_TOKEN):
        return jsonify({
            "status": True,
            "creator": CREATOR,
            "data": {"token": _AUTO_TOKEN, "expires_in": TOKEN_TTL_SECONDS},
        })
    return _error("Auto-login failed — check EMAIL/PASSWORD.", 500)


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
        return _error("Couldn't resolve a stream URL for that link.", 502)

    try:
        qualities = faphouse.get_available_qualities(video_url)
    except Exception as e:
        logger.warning(f"get_available_qualities failed: {e}")
        qualities = [{"label": "Auto (Best)", "height": None, "url": None}]

    title = meta.get("title")
    filename = _sanitize_filename(title) if title else _slug_filename(video_url)
    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"

    from urllib.parse import urlencode, quote_plus
    # Use full absolute URL so callers don't have to guess the host
    host = request.host_url.rstrip("/")
    base_dl = f"{host}/api/download?" + urlencode({"url": video_url, "token": token})
    quality_data = []
    for q in qualities:
        q_url = q.get("url")
        dl_url = base_dl + (f"&stream_url={quote_plus(q_url)}" if q_url else "")
        quality_data.append({
            "label":        q["label"],
            "height":       q.get("height"),
            "stream_url":   q_url or m3u8_url,
            "download_url": dl_url,
        })

    return jsonify({
        "status": True,
        "creator": CREATOR,
        "data": {
            "Filename":     filename,
            "thumbnail":    meta.get("poster_url"),
            "m3u8_link":    m3u8_url,
            "qualities":    quality_data,
            "download_url": base_dl,
        },
    })


@app.route("/api/download", methods=["GET"])
def download_video():
    """
    GET /api/download?url=<link>&token=<token>[&stream_url=<m3u8_url>]
    Muxes HLS via ffmpeg and streams mp4 as attachment download.
    """
    import subprocess
    from flask import Response, stream_with_context

    token = _get_bearer_token()
    if not token or not _session_for_token(token):
        return _error("Missing or expired token — log in first via POST /api/login.", 401)

    video_url  = (request.args.get("url") or "").strip()
    stream_url = (request.args.get("stream_url") or "").strip()

    if not video_url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(video_url):
        return _error("Not a faphouse.com/faphouse2.com link.")

    if not stream_url:
        try:
            stream_url = faphouse.client.get_m3u8_url(video_url)
        except Exception as e:
            return _error(f"Stream resolve failed: {e}", 502)
    if not stream_url:
        return _error("Could not resolve stream URL.", 502)

    try:
        meta  = faphouse.get_page_meta(video_url) or {}
        title = meta.get("title")
    except Exception:
        title = None
    filename = _sanitize_filename(title) if title else _slug_filename(video_url)
    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"

    cmd = [
        "ffmpeg", "-y",
        "-headers", "Referer: https://faphouse.com/\r\nUser-Agent: Mozilla/5.0\r\n",
        "-i", stream_url,
        "-c", "copy",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ]
    logger.info(f"[download] ffmpeg: {filename}")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return _error("ffmpeg not found on this server.", 500)

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()

    return Response(
        stream_with_context(generate()),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type":        "video/mp4",
            "X-Filename":          filename,
        },
        direct_passthrough=True,
    )


if __name__ == "__main__":
    _auto_login()
    port = int(os.environ.get("PORT", 8080))
    # threaded=True: resolving a link does blocking network I/O (session/
    # login, page fetch) — without this, Flask's dev server handles one
    # request at a time and a slow resolve would stall every other
    # request behind it. For real production traffic beyond this dev
    # server, run behind gunicorn with multiple workers instead (see
    # Dockerfile).
    app.run(host="0.0.0.0", port=port, threaded=True)

# Auto-login when loaded by gunicorn
_auto_login()
