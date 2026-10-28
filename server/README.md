# CELT News – LinkedIn feed server

Replaces the old Curator.io widget. This small Flask app holds the LinkedIn
token and serves the latest MEF University CELT posts
(`urn:li:organization:80317768`) as JSON. The page (`../index.html`) only talks
to this server, never to LinkedIn.

## 1. LinkedIn app setup (one time)

On the developer app's **Auth** tab, add these under *Authorized redirect URLs*:

```
http://localhost:5000/auth/callback
https://<production-host>/auth/callback
```

The app needs the Community Management API scopes
`r_organization_social` and `rw_organization_admin`.

## 2. Run locally

```bash
cd server
pip install -r requirements.txt
cp .env.example .env      # fill in LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET
python app.py
```

1. Open <http://localhost:5000/auth/login> in a browser where you are signed in
   to LinkedIn as a **Super admin of the CELT page**. The server checks the role
   and refuses to save a token from anyone else.
2. <http://localhost:5000/api/orgs> should list `urn:li:organization:80317768`.
3. <http://localhost:5000/> shows the CELT News page with the live feed.

## 3. Production

- Set `LINKEDIN_REDIRECT_URI=https://<production-host>/auth/callback` in `.env`.
- Run with one worker so the cache and refresh logic are shared:
  `gunicorn -w 1 --threads 4 -b 0.0.0.0:5000 app:app`
  (on Windows: `pip install waitress` then `waitress-serve --port=5000 app:app`).
- If the page is hosted elsewhere (e.g. embedded in Google Sites), set
  `LINKEDIN_FEED_API` near the bottom of the page template to this server's
  URL, e.g. `https://celt-feed.mef.edu.tr`. The page must be HTTPS-to-HTTPS.
- `server/data/` holds the token and post cache. Keep it out of git (it is in
  `.gitignore`) and on persistent storage.

## Routes

| Route | Purpose |
| --- | --- |
| `/api/posts` | Latest public posts (drafts and ad-only posts excluded). Refreshed from LinkedIn at most once per `CACHE_TTL`. If LinkedIn fails, the last good posts keep being served. |
| `/api/health` | Days left on the access and refresh tokens, last fetch, last error. |
| `/api/orgs` | Pages the stored token administers (setup check). |
| `/auth/login` | Re-authorize. Must be done by a CELT page admin. |

## Yearly maintenance (put these in a calendar)

1. **Refresh token expires ~365 days after login.** After that the feed stops
   updating (old posts keep showing). A page admin must open `/auth/login`
   again. `/api/health` shows `refresh_token_days_left` and flips
   `reauthorize_soon` to `true` in the last 30 days. Set a reminder at 11 months.
2. **`LINKEDIN_API_VERSION` is sunset after about a year.** When it is, every
   call fails at once. Bump it in `.env` (format `YYYYMM`) annually.
