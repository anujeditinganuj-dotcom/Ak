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
from urllib.parse import urlparse

from flask import Flask, jsonify, request

import faphouse_downloader as faphouse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("faphouse_api")

app = Flask(__name__)

# Optional — if set, requests must include a matching ?key=... or
# X-API-Key header. Left unset, the API is open (matches the reference
# AK API's own behavior, which has no visible auth either).
API_KEY = os.environ.get("API_KEY", "").strip()

CREATOR = "FapHouse API"


def _check_api_key() -> bool:
    if not API_KEY:
        return True
    supplied = request.args.get("key") or request.headers.get("X-API-Key")
    return supplied == API_KEY


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
        "message": "GET /api/faphouse?url=<faphouse.com or faphouse2.com video link>",
    })


@app.route("/api/faphouse", methods=["GET"])
def resolve_faphouse():
    if not _check_api_key():
        return _error("Invalid or missing API key.", 401)

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
