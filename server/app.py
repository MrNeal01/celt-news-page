"""
CELT News – LinkedIn feed server.

Keeps the LinkedIn token on the server and exposes the latest MEF University
CELT page posts as plain JSON at /api/posts. The browser never talks to
LinkedIn directly.

Routes
  /                 the CELT News page (../index.html), handy for local testing
  /auth/login       start OAuth as a Super admin of the CELT LinkedIn page
  /auth/callback    OAuth redirect target (must be added on the app's Auth tab)
  /api/posts        cached posts, refreshed from LinkedIn at most once per CACHE_TTL
  /api/health       token lifetime, last fetch, last error
  /api/orgs         organizations the stored token administers (setup check)
"""

import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, request, send_file, session

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

CLIENT_ID = os.environ.get("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINKEDIN_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("LINKEDIN_REDIRECT_URI", "http://localhost:5000/auth/callback")
ORG_ID = os.environ.get("LINKEDIN_ORG_ID", "80317768")
API_VERSION = os.environ.get("LINKEDIN_API_VERSION", "202610")
SCOPES = os.environ.get("LINKEDIN_SCOPES", "r_organization_social rw_organization_admin")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))
POSTS_LIMIT = int(os.environ.get("POSTS_LIMIT", "6"))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
PAGE_FILE = BASE_DIR.parent / "index.html"

ORG_URN = f"urn:li:organization:{ORG_ID}"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
REST = "https://api.linkedin.com/rest"

TOKEN_FILE = DATA_DIR / "token.json"
CACHE_FILE = DATA_DIR / "posts_cache.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or hashlib.sha256(
    f"celt-feed:{CLIENT_SECRET}".encode()
).hexdigest()

_lock = threading.Lock()
_state = {"refreshing": False, "last_error": None, "last_attempt": 0}


# ── Storage ──────────────────────────────────────────────────────────────────

def _read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def _write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_cache = _read_json(CACHE_FILE, {"fetched_at": 0, "posts": []})


# ── OAuth tokens ─────────────────────────────────────────────────────────────

def _save_token(tok, previous=None):
    now = int(time.time())
    previous = previous or {}
    data = {
        "access_token": tok["access_token"],
        "expires_at": now + int(tok.get("expires_in", 0)),
        "scope": tok.get("scope", previous.get("scope", "")),
        "authorized_at": previous.get("authorized_at", now),
        # A refresh keeps the original refresh-token expiry; LinkedIn does not extend it.
        "refresh_token": tok.get("refresh_token", previous.get("refresh_token")),
        "refresh_token_expires_at": (
            now + int(tok["refresh_token_expires_in"])
            if "refresh_token_expires_in" in tok
            else previous.get("refresh_token_expires_at")
        ),
    }
    _write_json(TOKEN_FILE, data)
    return data


def _refresh_access_token(tok):
    if not tok.get("refresh_token") or (tok.get("refresh_token_expires_at") or 0) < time.time():
        raise RuntimeError("Refresh token missing or expired: a page admin must visit /auth/login again.")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({r.status_code}): {r.text[:200]}")
    return _save_token(r.json(), tok)


def _access_token(force_refresh=False):
    tok = _read_json(TOKEN_FILE)
    if not tok:
        raise RuntimeError("Not authorized yet: a page admin must visit /auth/login.")
    # Refresh a day early so a slow week of traffic never hits an expired token.
    if force_refresh or tok["expires_at"] - time.time() < 86400:
        tok = _refresh_access_token(tok)
    return tok["access_token"]


def _li_get(path, query=""):
    """GET a LinkedIn REST endpoint. `query` is a pre-encoded query string."""
    url = f"{REST}/{path}" + (f"?{query}" if query else "")
    for attempt in (0, 1):
        r = requests.get(url, headers={
            "Authorization": f"Bearer {_access_token(force_refresh=attempt == 1)}",
            "LinkedIn-Version": API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        }, timeout=20)
        if r.status_code != 401:
            break
    if r.status_code != 200:
        raise RuntimeError(f"LinkedIn {path} returned {r.status_code}: {r.text[:300]}")
    return r.json()


def _batch_get(path, urns):
    if not urns:
        return {}
    ids = ",".join(quote(u, safe="") for u in urns)
    return _li_get(path, f"ids=List({ids})").get("results", {})


def _admin_orgs(access_token):
    r = requests.get(
        f"{REST}/organizationAcls",
        params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "LinkedIn-Version": API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
        },
        timeout=20,
    )
    r.raise_for_status()
    return [e.get("organization") or e.get("organizationTarget") for e in r.json().get("elements", [])]


# ── Post parsing ─────────────────────────────────────────────────────────────

_MENTION = re.compile(r"@\[((?:\\.|[^\]\\])*)\]\(urn:li:[^)]+\)")
_HASHTAG = re.compile(r"\{hashtag\|\\?#\|((?:\\.|[^}\\])*)\}")
_ESCAPE = re.compile(r"\\(.)")


def little_text_to_plain(text):
    """LinkedIn 'little text' commentary → plain text (mentions, hashtags, escapes)."""
    text = _HASHTAG.sub(r"#\1", text or "")
    text = _MENTION.sub(r"\1", text)
    return _ESCAPE.sub(r"\1", text).strip()


def _media_urns(post):
    content = post.get("content") or {}
    if "media" in content:
        return [content["media"].get("id")]
    if "multiImage" in content:
        return [img.get("id") for img in content["multiImage"].get("images", [])]
    if "article" in content and content["article"].get("thumbnail"):
        return [content["article"]["thumbnail"]]
    return []


def _is_public(post):
    return (
        post.get("lifecycleState") == "PUBLISHED"
        and post.get("visibility") == "PUBLIC"
        # Dark posts (ads that never appear on the page) have feedDistribution NONE.
        and (post.get("distribution") or {}).get("feedDistribution") != "NONE"
    )


def parse_posts(elements, images, videos):
    posts = []
    for p in elements:
        if not _is_public(p):
            continue
        content = p.get("content") or {}
        urns = [u for u in _media_urns(p) if u]
        image_urls = []
        for u in urns:
            if u.startswith("urn:li:image:") and images.get(u, {}).get("downloadUrl"):
                image_urls.append(images[u]["downloadUrl"])
            elif u.startswith("urn:li:video:") and videos.get(u, {}).get("thumbnail"):
                image_urls.append(videos[u]["thumbnail"])

        article = content.get("article")
        post = {
            "id": p["id"],
            "url": f"https://www.linkedin.com/feed/update/{p['id']}/",
            "text": little_text_to_plain(p.get("commentary")),
            "published_at": p.get("publishedAt") or p.get("createdAt"),
            "images": image_urls[:4],
            "image_count": len(urns) if not article else 0,
            "is_video": any(u.startswith("urn:li:video:") for u in urns),
            "article": {
                "url": article.get("source"),
                "title": article.get("title"),
            } if article else None,
        }
        if post["text"] or post["images"] or post["article"]:
            posts.append(post)

    posts.sort(key=lambda x: x["published_at"] or 0, reverse=True)
    return posts[:POSTS_LIMIT]


def fetch_posts():
    query = urlencode({
        "author": ORG_URN, "q": "author", "count": max(POSTS_LIMIT * 2, 10), "sortBy": "CREATED",
    })
    elements = _li_get("posts", query).get("elements", [])
    public = [p for p in elements if _is_public(p)]
    urns = {u for p in public for u in _media_urns(p) if u}
    images = _batch_get("images", sorted(u for u in urns if u.startswith("urn:li:image:")))
    videos = _batch_get("videos", sorted(u for u in urns if u.startswith("urn:li:video:")))
    return parse_posts(elements, images, videos)


# ── Cache ────────────────────────────────────────────────────────────────────

def _refresh_cache():
    global _cache
    try:
        posts = fetch_posts()
        _cache = {"fetched_at": int(time.time()), "posts": posts}
        _write_json(CACHE_FILE, _cache)
        _state["last_error"] = None
    except Exception as e:  # keep serving the last good posts
        _state["last_error"] = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {e}"
        app.logger.warning("LinkedIn refresh failed: %s", e)
    finally:
        _state["refreshing"] = False


def _maybe_refresh():
    """Stale-while-revalidate: refresh in the background, block only on an empty cache."""
    now = time.time()
    with _lock:
        stale = now - _cache["fetched_at"] > CACHE_TTL
        # After a failure, wait 5 minutes before hitting LinkedIn again.
        cooling = _state["last_error"] and now - _state["last_attempt"] < 300
        if not stale or cooling or _state["refreshing"] or not TOKEN_FILE.exists():
            return
        _state["refreshing"] = True
        _state["last_attempt"] = now
    if _cache["posts"]:
        threading.Thread(target=_refresh_cache, daemon=True).start()
    else:
        _refresh_cache()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.after_request
def _cors(resp):
    if request.path.startswith("/api/"):
        allowed = [o.strip() for o in ALLOWED_ORIGIN.split(",") if o.strip()]
        origin = request.headers.get("Origin")
        if "*" in allowed:
            resp.headers["Access-Control-Allow-Origin"] = "*"
        elif origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
    return resp


@app.get("/")
def page():
    if not PAGE_FILE.exists():
        abort(404)
    return send_file(PAGE_FILE)


@app.get("/auth/login")
def auth_login():
    if not CLIENT_ID or not CLIENT_SECRET:
        return "LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET are not set in server/.env", 500
    session["oauth_state"] = secrets.token_urlsafe(24)
    return redirect(AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": session["oauth_state"],
        "scope": SCOPES,
    }))


@app.get("/auth/callback")
def auth_callback():
    if request.args.get("error"):
        return f"LinkedIn returned an error: {request.args.get('error_description') or request.args['error']}", 400
    if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
        return "Login session expired or state mismatch. Start again at /auth/login.", 400

    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": request.args.get("code", ""),
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, timeout=20)
    if r.status_code != 200:
        return f"Token exchange failed ({r.status_code}): {r.text[:300]}", 400
    tok = r.json()

    # Only store the token if this person actually administers the CELT page;
    # otherwise anyone could visit /auth/login and break the feed.
    try:
        orgs = _admin_orgs(tok["access_token"])
    except requests.RequestException as e:
        return f"Could not verify page admin role: {e}", 400
    if ORG_URN not in orgs:
        return (f"This LinkedIn account is not an approved admin of {ORG_URN}. "
                f"Admin of: {orgs or 'none'}. Nothing was saved."), 403

    saved = _save_token(tok, _read_json(TOKEN_FILE))
    _cache["fetched_at"] = 0
    _state["last_error"] = None
    days = (saved["refresh_token_expires_at"] - time.time()) / 86400 if saved["refresh_token_expires_at"] else None
    return jsonify({
        "ok": True,
        "organization": ORG_URN,
        "refresh_token_days_left": round(days) if days else None,
        "next": "Open /api/posts to load the feed.",
    })


@app.get("/api/posts")
def api_posts():
    _maybe_refresh()
    resp = jsonify({
        "organization": ORG_URN,
        "page_url": "https://www.linkedin.com/company/mef-celt",
        "fetched_at": _cache["fetched_at"],
        "posts": _cache["posts"],
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/api/health")
def api_health():
    tok = _read_json(TOKEN_FILE) or {}
    now = time.time()

    def days(ts):
        return round((ts - now) / 86400, 1) if ts else None

    refresh_days = days(tok.get("refresh_token_expires_at"))
    return jsonify({
        "authorized": bool(tok),
        "organization": ORG_URN,
        "api_version": API_VERSION,
        "access_token_days_left": days(tok.get("expires_at")),
        "refresh_token_days_left": refresh_days,
        "reauthorize_soon": refresh_days is not None and refresh_days < 30,
        "last_fetch": datetime.fromtimestamp(_cache["fetched_at"], timezone.utc).isoformat()
        if _cache["fetched_at"] else None,
        "post_count": len(_cache["posts"]),
        "last_error": _state["last_error"],
    })


@app.get("/api/orgs")
def api_orgs():
    try:
        return jsonify({"admin_of": _admin_orgs(_access_token())})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
