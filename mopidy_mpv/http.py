"""Tiny HTTP app that serves embedded cover art for local mpv: tracks.

Mopidy mounts this under /mpv/ on the HTTP server, so the library provider's
get_images() can hand frontends (Iris) a fetchable URL — /mpv/cover?path=... —
that returns the JPEG/PNG embedded in the audio file. Without this, web clients
have no way to load artwork for files we play through the mpv: scheme (we don't
use Mopidy-Local, which is what normally serves local images).
"""

import logging
import os
import re
import secrets
import subprocess
from urllib.parse import unquote

import tornado.web

logger = logging.getLogger(__name__)

_AUDIO_EXT = (".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".oga", ".flac")

_COOKIES_DIR = os.path.expanduser("~/.config/yt-dlp/cookies")
_YT_PROFILE = os.path.expanduser("~/.local/bin/yt-profile")
_ACTIVE_BOOK = os.path.join(_COOKIES_DIR, "active-book.txt")
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")

_TV_VIDEO = os.path.expanduser("~/.local/bin/tv-video")


def extract_cover(path):
    """(bytes, mime) for the file's embedded cover, or (None, None)."""
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(path)
        if mf is None:
            return None, None
        tags = getattr(mf, "tags", None)
        # MP4 / M4B / M4A: 'covr' atom.
        if tags and "covr" in tags:
            import mutagen.mp4 as mp4
            covr = tags["covr"][0]
            mime = ("image/png"
                    if getattr(covr, "imageformat", None) == mp4.MP4Cover.FORMAT_PNG
                    else "image/jpeg")
            return bytes(covr), mime
        # ID3 (mp3): APIC frame.
        if tags:
            for key in tags.keys():
                if key.startswith("APIC"):
                    apic = tags[key]
                    return apic.data, (apic.mime or "image/jpeg")
        # FLAC / Ogg: picture blocks.
        pics = getattr(mf, "pictures", None)
        if pics:
            return pics[0].data, (pics[0].mime or "image/jpeg")
    except Exception:  # noqa: BLE001 — artwork is best-effort
        logger.debug("cover extract failed for %s", path, exc_info=True)
    return None, None


class CoverHandler(tornado.web.RequestHandler):
    def get(self):
        path = unquote(self.get_argument("path", ""))
        # Only serve embedded artwork from real audio files (no arbitrary reads).
        if not (path.startswith("/") and os.path.isfile(path)
                and path.lower().endswith(_AUDIO_EXT)):
            self.set_status(404)
            return
        data, mime = extract_cover(path)
        if not data:
            self.set_status(404)
            return
        self.set_header("Content-Type", mime)
        self.set_header("Cache-Control", "public, max-age=86400")
        self.write(data)


class BookProfileHandler(tornado.web.RequestHandler):
    """Switch the BOOK channel's YouTube account from the web UI (Iris Commands).

    GET /mpv/ytbook            -> {"current": "<name>"} (book-channel profile)
    GET /mpv/ytbook?name=<n>   -> repoint active-book.txt and (in the background)
                                  regenerate that account's playlist pickers.
                                  Returns immediately; pickers appear on the next
                                  Iris playlists Refresh (the m3u backend re-scans
                                  live, so no Mopidy restart is needed).

    Only names matching an existing `<name>.txt` cookie jar are accepted, so the
    argument can't traverse paths or inject a command (passed as a list to Popen).
    """

    def _current(self):
        try:
            return os.path.basename(os.readlink(_ACTIVE_BOOK))[:-4]
        except OSError:
            return None

    def get(self):
        name = self.get_argument("name", "")
        if not name:
            # No name -> current + the full selectable list (for the in-Iris
            # account popover to build its dropdown without a separate page).
            self.write({"current": self._current(),
                        "profiles": [{"name": n, "oauth": o}
                                     for n, _cur, o in _book_profiles()]})
            return
        if not _NAME_RE.match(name) or not os.path.isfile(
                os.path.join(_COOKIES_DIR, f"{name}.txt")):
            self.set_status(400)
            self.write({"ok": False, "error": f"unknown profile {name!r}"})
            return
        # Fire-and-forget: `yt-profile book use` repoints + regenerates pickers
        # (no restart). Detached so this request returns instantly even though a
        # big account takes ~1-2 min to enumerate.
        subprocess.Popen(
            [_YT_PROFILE, "book", "use", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.write({"ok": True, "name": name,
                    "note": "switching — Refresh the Iris playlists view shortly"})


def _is_oauth(name):
    """True if <name> is a Google-login (OAuth) profile (token has a
    refresh_token), vs a cookie-derived one."""
    try:
        with open(os.path.join(_COOKIES_DIR, name + ".json")) as jf:
            return '"refresh_token"' in jf.read()
    except OSError:
        return False


def _book_profiles():
    """[(name, is_current, is_oauth)] of selectable book profiles + 'none'.

    OAuth profiles ARE included now: their playlists are enumerated via the
    YouTube Data API v3 (gen-youtube-books-m3u's OAuth path), which — unlike
    ytmusicapi's InnerTube — honours these tokens. They're added via the
    "Add account" Google-login flow (/mpv/ytadd)."""
    try:
        current = os.path.basename(os.readlink(_ACTIVE_BOOK))[:-4]
    except OSError:
        current = "none"
    out = []
    try:
        for f in sorted(os.listdir(_COOKIES_DIR)):
            if not f.endswith(".txt"):
                continue
            n = f[:-4]
            if n.startswith("active") or n == "none":
                continue
            out.append((n, n == current, _is_oauth(n)))
    except OSError:
        pass
    out.append(("none", current == "none", False))
    return out


# Standalone, phone-friendly switcher page. Bookmark it on Android; the <select>
# scales to any number of profiles. Talks to /mpv/ytbook + /mpv/ytadd (same
# origin). Uses a __OPTIONS__ placeholder (not str.format) so the JS braces stay
# readable. The "Add account" box runs the Google-login (OAuth device) flow.
_SWITCHER_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Book account</title><style>
 body{font-family:system-ui,sans-serif;max-width:480px;margin:1.5rem auto;padding:0 1.2rem;color:#222}
 h2{font-weight:600} h3{font-weight:600;font-size:1rem;margin:1.6rem 0 .5rem}
 select{font-size:1.15rem;padding:.6rem;width:100%;border-radius:.6rem;border:1px solid #bbb;box-sizing:border-box}
 button{font-size:1.05rem;padding:.6rem 1rem;margin-top:.6rem;border:0;border-radius:.6rem;background:#1b1b1b;color:#fff;cursor:pointer}
 button[disabled]{opacity:.5} button.ghost{background:#fff;color:#b00;border:1px solid #ddd}
 #back{background:none;color:#0a58ca;padding:.2rem 0;font-size:1rem;margin:0 0 .6rem}
 .row{display:flex;gap:.6rem} .row select{flex:1}
 #status,#addstatus{margin-top:1rem;min-height:1.4em;color:#555}
 .code{font:700 1.5rem/1.2 ui-monospace,monospace;letter-spacing:.12em;background:#f3f3f3;padding:.5rem .7rem;border-radius:.5rem;display:inline-block;margin:.3rem 0}
 a.go{display:inline-block;margin-top:.4rem} small{color:#999} hr{border:0;border-top:1px solid #eee;margin:1.6rem 0}
</style></head><body>
<button id=back>‹ Back</button>
<h2>\U0001F4D6 Book channel &middot; YouTube account</h2>
<div class=row><select id=sel>__OPTIONS__</select><button id=del class=ghost title="Remove this account">Remove</button></div>
<p id=status></p>
<p><small>Pick an account, then hit Refresh in the Iris playlists view. First load of a big account takes ~1&ndash;2&nbsp;min; switching back is instant.</small></p>
<hr>
<h3>➕ Add account &middot; Google login</h3>
<button id=add>Sign in with Google</button>
<div id=addstatus></div>
<p><small>No cookie upload needed — the account is named after your YouTube channel automatically, your playlists are read via YouTube's official API, and the login refreshes itself. (Shows your <em>own</em> playlists, not ones saved from other channels.)</small></p>
<script>
 function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
 // Standalone (bookmarked) page: offer Back to Iris. When embedded in the
 // in-Iris overlay frame, the overlay's × closes it, so hide Back.
 (function(){var b=document.getElementById('back');
  if(window.self!==window.top){b.style.display='none';}
  else{b.onclick=function(){ if(history.length>1){history.back();} else {location.href='/iris/';} };}})();
 var sel=document.getElementById('sel'),st=document.getElementById('status');
 sel.onchange=function(){
   st.textContent='Switching to '+sel.value+'\\u2026';
   fetch('/mpv/ytbook?name='+encodeURIComponent(sel.value)).then(function(r){return r.json()})
    .then(function(j){st.textContent=j.ok?(j.note||'Switched.'):('Error: '+(j.error||'failed'))})
    .catch(function(e){st.textContent='Error: '+e});
 };
 document.getElementById('del').onclick=function(){
   var n=sel.value;
   if(n==='none'){st.textContent='Nothing to remove.';return;}
   if(!confirm('Remove account "'+n+'"? This deletes its saved login and playlists from the book channel.'))return;
   st.textContent='Removing '+n+'\\u2026';
   fetch('/mpv/ytdel?name='+encodeURIComponent(n),{method:'POST'}).then(function(r){return r.json()})
    .then(function(j){ if(j.ok){st.textContent='Removed '+n+'. Reloading\\u2026';setTimeout(function(){location.reload()},900);}
      else {st.textContent='Error: '+(j.error||'failed');} })
    .catch(function(e){st.textContent='Error: '+e});
 };
 var addBtn=document.getElementById('add'),as=document.getElementById('addstatus'),poll=null;
 function render(j){
   if(j.state==='authorize'){
     as.innerHTML='<a class=go target=_blank rel=noopener href="'+esc(j.verification_url_complete||j.verification_url)+'" style="display:inline-block;background:#1b1b1b;color:#fff;padding:.5rem .9rem;border-radius:.5rem;text-decoration:none">Approve in Google \\u2192</a><br><small>code <b>'+esc(j.user_code||'')+'</b> is pre-filled \\u2014 just confirm</small>';
   } else if(j.state==='starting'){ as.textContent='Starting Google login\\u2026'; }
   else if(j.state==='enumerating'){ as.textContent='Approved \\u2713 (as '+esc(j.channel||'')+') — loading your playlists\\u2026'; }
   else if(j.state==='done'){ clearInterval(poll);poll=null;addBtn.disabled=false;
     as.innerHTML='\\u2713 Added <b>'+esc(j.channel||j.name||'')+'</b> with '+(j.playlists||0)+' playlists. Reloading\\u2026';
     setTimeout(function(){location.reload()},1600); }
   else if(j.state==='error'){ clearInterval(poll);poll=null;addBtn.disabled=false;
     as.textContent='Error: '+(j.message||'failed'); }
 }
 addBtn.onclick=function(){
   addBtn.disabled=true; as.textContent='Starting Google login\\u2026';
   fetch('/mpv/ytadd',{method:'POST'}).then(function(r){return r.json()})
    .then(function(j){ if(!j.ok||!j.handle){addBtn.disabled=false;as.textContent='Error: '+(j.error||'failed');return;}
      var h=j.handle;
      poll=setInterval(function(){
        fetch('/mpv/ytadd?h='+encodeURIComponent(h)).then(function(r){return r.json()}).then(render).catch(function(){});
      },2500);
    }).catch(function(e){addBtn.disabled=false;as.textContent='Error: '+e});
 };
</script></body></html>"""


class BookSwitcherHandler(tornado.web.RequestHandler):
    def get(self):
        opts = "".join(
            '<option value="{n}"{sel}>{label}</option>'.format(
                n=n, sel=(" selected" if cur else ""),
                label=(n + " — off" if n == "none"
                       else (n + " · Google" if oauth else n)))
            for n, cur, oauth in _book_profiles())
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.write(_SWITCHER_HTML.replace("__OPTIONS__", opts))


_YT_GOOGLE_ADD = os.path.expanduser("~/.local/bin/yt-google-add")
_YT_PROFILE_BIN = os.path.expanduser("~/.local/bin/yt-profile")
_YTADD_STATUS = os.path.expanduser("~/.local/state/agent-media/ytadd")
_HANDLE_RE = re.compile(r"\A[0-9a-f]{8,32}\Z")


class YtAddHandler(tornado.web.RequestHandler):
    """Add a book profile via Google login (OAuth device flow).

    POST /mpv/ytadd        -> mint an opaque handle, kick off yt-google-add in
                              the background (the profile is named after the
                              signed-in channel, so no name is given here);
                              returns {ok, handle} at once.
    GET  /mpv/ytadd?h=<h>  -> the current status JSON the page polls
                              (state: starting|authorize|enumerating|done|error;
                              done carries {name, channel, playlists}).
    """

    def get(self):
        handle = self.get_argument("h", "")
        self.set_header("Content-Type", "application/json")
        if not _HANDLE_RE.match(handle):
            self.set_status(400)
            self.write({"state": "error", "message": "bad handle"})
            return
        try:
            with open(os.path.join(_YTADD_STATUS, handle + ".json")) as f:
                self.write(f.read())
        except OSError:
            self.write({"state": "idle"})

    def post(self):
        handle = secrets.token_hex(8)
        subprocess.Popen(
            [_YT_GOOGLE_ADD, handle],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.write({"ok": True, "handle": handle})


class YtDelHandler(tornado.web.RequestHandler):
    """Remove a book profile (its login + cached playlists).

    POST /mpv/ytdel?name=<n> -> `yt-profile book rm <n>` (refuses none/active;
    if it was the active book profile, falls back to 'none' and clears pickers).
    """

    def post(self):
        name = self.get_argument("name", "")
        if (not _NAME_RE.match(name)
                or name in ("none", "active", "active-book")):
            self.set_status(400)
            self.write({"ok": False, "error": "invalid name"})
            return
        if not os.path.isfile(os.path.join(_COOKIES_DIR, name + ".txt")):
            self.set_status(404)
            self.write({"ok": False, "error": "no such profile"})
            return
        p = subprocess.run(
            [_YT_PROFILE_BIN, "book", "rm", name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if p.returncode != 0:
            self.set_status(500)
            self.write({"ok": False, "error": (p.stdout or "").strip()[:200]})
            return
        self.write({"ok": True, "name": name})


class TvVideoHandler(tornado.web.RequestHandler):
    """Play the currently-playing track on the lounge TV's mpvKt (TV-only audio).

    GET /mpv/tv            -> play the NOW-PLAYING track on the TV.
    GET /mpv/tv?uri=<u>    -> play a SPECIFIC track (the one a context menu was
                             opened on) on the TV, regardless of what's playing.

    Either way: stop Mopidy's audio (so the am-music Snapcast feed goes quiet and
    the TV isn't double-audioed) and hand the URI to ~/.local/bin/tv-video, which
    resolves it (YouTube via the TV's own yt-dlp) and fires it into mpvKt.
    Fire-and-forget; returns at once. Our own `mpv:` wrapper is stripped first so
    book-channel tracks (mpv:https://…) work, not just bare youtube:/http URIs.
    """

    def initialize(self, core):
        self.core = core

    def get(self):
        uri = self.get_argument("uri", "")
        if not uri:
            tl = self.core.playback.get_current_tl_track().get()
            if tl is None:
                self.set_status(409)
                self.write({"ok": False, "error": "nothing is playing"})
                return
            uri = tl.track.uri
        # Drop our scheme wrapper so the check + tv-video see the real media URI
        # (mpv:https://… -> https://…, mpv:youtube:video:ID -> youtube:video:ID).
        clean = uri[len("mpv:"):] if uri.startswith("mpv:") else uri
        if not (clean.startswith(("yt:", "youtube:", "http://", "https://",
                                  "file://")) or clean.startswith("/")):
            self.set_status(415)
            self.write({"ok": False, "error": "not a video track", "uri": uri})
            return
        # Quiet Mopidy so am-music stops; the TV plays the video with its own
        # (perfectly synced) audio via mpvKt.
        try:
            self.core.playback.stop()
        except Exception:  # noqa: BLE001
            logger.debug("core.playback.stop() failed", exc_info=True)
        subprocess.Popen(
            [_TV_VIDEO, clean],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.write({"ok": True, "uri": uri, "note": "playing on TV"})


def mpv_http_factory(config, core):
    # Mounted at /mpv/ -> /mpv/cover, /mpv/ytbook (switch API), /mpv/books (UI),
    # /mpv/tv (play now-playing on the lounge TV's mpvKt).
    return [(r"/cover", CoverHandler),
            (r"/ytbook", BookProfileHandler),
            (r"/ytadd", YtAddHandler),
            (r"/ytdel", YtDelHandler),
            (r"/books", BookSwitcherHandler),
            (r"/tv", TvVideoHandler, dict(core=core))]
