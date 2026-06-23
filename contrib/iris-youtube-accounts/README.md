# Add / switch YouTube accounts in Mopidy-Iris via Google login

A small, optional add-on that puts a **YouTube account switcher** into
Mopidy-Iris's own sidebar: a popover with a dropdown to switch the active
account, a **"Add account" button that signs in with Google**, and a Remove.
No cookie-jar uploads, no separate page, no Iris fork.

> Extracted from the author's "book channel" setup. The backend HTTP routes ship
> with this plugin (`mopidy_mpv/http.py`); the three scripts here are the
> front-end + the OAuth login tool, de-personalized so you can adapt them. What
> you *do* with a logged-in account (enumerate its playlists into Mopidy) is the
> one part you wire yourself — see [Plugging in your enumerator](#plugging-in-your-enumerator).

## How it looks

Sidebar gets an **Account** item → clicking it opens:

```
┌─ 📺 YouTube account ──────────┐
│ [ my-channel · Google      ▾] │
│ [ Add account ]   [ Remove ]  │
│ ✓ Added My Channel (64 pl…)   │
└───────────────────────────────┘
```

"Add account" → a Google sign-in tab opens → you approve → the account appears
with its playlists. Fully code-free when the web-app client is configured;
otherwise it falls back to a one-tap device-code flow.

## Three techniques worth stealing even if you don't use the whole thing

1. **Custom nav in Iris with no fork** — Iris has no plugin hook for sidebar
   items, so `iris-inject-accounts` patches the *served* `index.html` with a
   `<script>` that injects a native-looking `sidebar__menu__item` via a
   `MutationObserver` (re-added on every React re-render). `iris-inject-sw`
   patches Iris's cache-first service worker to `skipWaiting` + serve navigations
   network-first, so injected changes propagate without a manual cache clear.
2. **Enumerate a YouTube account's playlists with an OAuth token via the
   official Data API v3** (`playlists.list?mine=true` + `playlistItems.list`).
   This sidesteps `ytmusicapi`'s InnerTube endpoints, which return HTTP 400 for
   "TVs and Limited Input devices" tokens.
3. **A code-free OAuth redirect flow on a headless box** — `tailscale serve`
   gives a `https://<host>.ts.net` URL with a real cert to use as the OAuth
   redirect URI, so the auth-code flow works without exposing anything publicly.

## Components

| Piece | Where | Role |
|---|---|---|
| `YtAddHandler` / `YtCbHandler` / `YtDelHandler`, `_web_client`, `_write_status` | `mopidy_mpv/http.py` (this plugin) | `/mpv/ytadd` (start + poll), `/mpv/ytcb` (OAuth redirect target), `/mpv/ytdel` (remove) |
| `_book_profiles` / `BookProfileHandler` | `mopidy_mpv/http.py` | `/mpv/ytbook` — list/switch the active account (adapt to your own "active account" model) |
| `iris-inject-accounts` | here | injects the sidebar item + popover into Iris's `index.html` |
| `iris-inject-sw` | here | makes Iris's service worker self-update so the injection shows |
| `yt-google-add` | here | runs the Google login (device **or** auth-code flow), writes the token, runs your activate hook |

Request flow for "Add account":

```
popover  --POST /mpv/ytadd-->  YtAddHandler
                                 ├─ web client set?  → returns {auth_url}; popover opens it
                                 │     Google → GET /mpv/ytcb?code&state → yt-google-add --code …
                                 └─ else            → spawns yt-google-add <handle> (device flow)
popover  --GET /mpv/ytadd?h=…-->  polls the status file yt-google-add writes
                                  (authorize → enumerating → done)
```

## Setup

### 1. OAuth clients (Google Cloud Console)

Create a project, enable **YouTube Data API v3**, and create up to two OAuth
client IDs (same consent screen):

- **TVs and Limited Input devices** → for the device flow (always works, shows a
  code). Save it: write `{"client_id":…,"client_secret":…}` to
  `$YT_OAUTH_DIR/oauth_client.json`.
- **Web application** (optional, for the code-free flow) → add an **Authorized
  redirect URI** of `https://<your-host>/mpv/ytcb`. Save it: write
  `{"client_id":…,"client_secret":…,"redirect_uri":"https://<your-host>/mpv/ytcb"}`
  to `$YT_OAUTH_DIR/oauth_web.json`.

`$YT_OAUTH_DIR` defaults to `~/.config/yt-dlp/cookies`. The web client makes the
flow fully code-free; without it everything still works via the device flow.

Notes:
- `youtube.readonly` is a **sensitive** scope, so an unverified app shows a
  "Google hasn't verified this app" interstitial (Advanced → continue) to
  non-owner users. Fine for personal use; only matters if others sign in.
- While the consent screen is in **Testing**, refresh tokens expire after ~7
  days. **Publish** it (Audience → Publish app) for durable tokens.

### 2. HTTPS for the redirect URI (only for the code-free flow)

Google requires the redirect URI to be `https://` (or `http://localhost`).
Easiest on a headless/home box is Tailscale Serve:

```
tailscale serve --bg --https=8443 http://127.0.0.1:6680   # → your Iris/Mopidy port
# redirect URI then = https://<host>.<tailnet>.ts.net:8443/mpv/ytcb
```

Any HTTPS reverse proxy that reaches this plugin's HTTP routes works too.

### 3. Inject the UI + wire it into Mopidy

Run both injectors with the Python that has `mopidy_iris` installed, and re-run
them on every Mopidy start (Iris upgrades overwrite `index.html`):

```ini
# ~/.config/systemd/user/mopidy.service.d/iris-yt.conf  (or your service)
[Service]
ExecStartPre=/path/to/iris-inject-accounts
ExecStartPre=/path/to/iris-inject-sw      # MUST run after the line above
```

Env knobs (all optional): `IRIS_YT_PORT` (restrict injection to one Iris port),
`YT_OAUTH_DIR`, `YT_STATUS_DIR`, `YT_ACTIVATE_CMD`, `YT_PLAYLISTS_DIR`.

## Plugging in your enumerator

`yt-google-add` only handles the login: it writes `$YT_OAUTH_DIR/<name>.json`
(an auto-refreshing token, named after the channel, stamped with its issuing
client) and a cookieless `<name>.txt`, then runs **`$YT_ACTIVATE_CMD <name>`** —
that's where you turn the account into something Mopidy plays. A minimal
enumerator (the technique from #2 above):

```python
import json, os, urllib.parse, urllib.request

OAUTH_DIR = os.path.expanduser("~/.config/yt-dlp/cookies")

def access_token(name):
    t = json.load(open(f"{OAUTH_DIR}/{name}.json"))
    # refresh with the client that ISSUED the token (a refresh token only works
    # with its own client): web-app for issuer 'web', else the device client.
    client = "oauth_web.json" if t.get("issuer") == "web" else "oauth_client.json"
    c = json.load(open(f"{OAUTH_DIR}/{client}"))
    data = urllib.parse.urlencode({
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}).encode()
    return json.load(urllib.request.urlopen(
        "https://oauth2.googleapis.com/token", data))["access_token"]

def pages(at, path, params):
    tok = None
    while True:
        q = dict(params, maxResults=50, **({"pageToken": tok} if tok else {}))
        req = urllib.request.Request(
            f"https://www.googleapis.com/youtube/v3/{path}?" + urllib.parse.urlencode(q),
            headers={"Authorization": "Bearer " + at})
        page = json.load(urllib.request.urlopen(req))
        yield from page.get("items", [])
        tok = page.get("nextPageToken")
        if not tok:
            break

at = access_token("my-channel")
for pl in pages(at, "playlists", {"part": "snippet", "mine": "true"}):
    title, pid = pl["snippet"]["title"], pl["id"]
    items = pages(at, "playlistItems", {"part": "contentDetails", "playlistId": pid})
    video_ids = [it["contentDetails"]["videoId"] for it in items]
    # → write an .m3u8 / register a Mopidy playlist however your backend wants
```

The reference implementation writes one `YT - <title>.m3u8` of `mpv:` URIs per
playlist into a Mopidy `M3U` directory; mopidy-mpv plays each entry. Adapt to
your backend.

### Caveats on coverage

- `playlists.list?mine=true` returns the playlists you **own** — not ones you
  *saved* from other channels (the cookie-jar `/feed/playlists` view gets those
  too). **Liked videos** and **uploads** are reachable via
  `channels.list(part=contentDetails).relatedPlaylists.{likes,uploads}`.
- **Watch history** is **not** available via the Data API (Google removed
  `relatedPlaylists.watchHistory` access years ago). It's reachable only via
  cookie auth (`yt-dlp :ythistory`) or a Google Takeout export — i.e. a
  cookie-jar profile, not an OAuth one.
