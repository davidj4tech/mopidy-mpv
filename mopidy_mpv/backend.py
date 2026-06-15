"""Mopidy backend that plays URIs through an external mpv process via JSON-IPC,
bypassing GStreamer entirely.

Scope: handles the `mpv:` URI scheme with a PlaybackProvider (mpv over IPC) and
a *minimal* LibraryProvider — just enough lookup() for frontends to enqueue
mpv: URIs (core rejects un-lookup-able URIs as "No such song"). It does NOT
browse or search; pair it with Mopidy-YouTube/Stream for discovery, and route
the things you want mpv to play through the mpv: scheme.
"""

import logging
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import pykka

from mopidy import backend, audio
from mopidy.models import Album, Artist, Track

from .mpv_ipc import MpvIPC

logger = logging.getLogger(__name__)

_YT_WATCH = "https://www.youtube.com/watch?v={}"

# Where the BOOK channel's YouTube pickers live (EXTM3Us written by
# gen-youtube-books-m3u, with #EXTINF titles). We mine those titles so Iris can
# show real names + thumbnails for mpv: YouTube tracks instead of bare URLs —
# library.lookup() otherwise has no metadata for a watch URL.
_YT_PLAYLISTS_DIR = os.path.expanduser("~/.local/share/mopidy-books/playlists")
_YT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")


def _youtube_id(body):
    """Extract an 11-char YouTube video id from an mpv: body, else None.

    Handles watch URLs (?v=ID), youtu.be/ID, and the youtube:/yt: wrapper forms
    (youtube:video:ID, yt:video:ID, youtube:ID, yt:ID).
    """
    for prefix in ("youtube:video:", "yt:video:", "youtube:", "yt:"):
        if body.startswith(prefix):
            rest = body[len(prefix):]
            if not rest.startswith(("http://", "https://", "ytdl://")):
                return rest if _YT_ID_RE.match(rest) else None
            body = rest
            break
    if body.startswith(("http://", "https://")):
        try:
            u = urlparse(body)
        except ValueError:
            return None
        if u.netloc.endswith("youtu.be"):
            vid = u.path.lstrip("/").split("/")[0]
            return vid if _YT_ID_RE.match(vid) else None
        if "youtube.com" in u.netloc:
            vid = (parse_qs(u.query).get("v") or [""])[0]
            return vid if _YT_ID_RE.match(vid) else None
    return None


# {video_id: title}, rebuilt from the picker m3u files when they change.
_yt_title_cache: dict = {}
_yt_title_mtime = [0.0]


def _yt_titles():
    """{video_id: title} mined from the book-channel picker m3u files (cached).

    Reparses only when the playlists dir's newest mtime changes, so a profile
    switch / refresh is picked up without a Mopidy restart.
    """
    try:
        entries = os.scandir(_YT_PLAYLISTS_DIR)
    except OSError:
        return _yt_title_cache
    files, newest = [], 0.0
    for e in entries:
        if e.name.endswith((".m3u8", ".m3u")):
            files.append(e.path)
            try:
                newest = max(newest, e.stat().st_mtime)
            except OSError:
                pass
    if newest == _yt_title_mtime[0] and _yt_title_cache:
        return _yt_title_cache
    titles = {}
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        pending = None
        for line in lines:
            if line.startswith("#EXTINF:"):
                pending = line.split(",", 1)[1].strip() if "," in line else None
            elif line and not line.startswith("#"):
                body = _mpv_body(line.strip())
                vid = _youtube_id(body)
                if vid and pending:
                    titles[vid] = pending
                pending = None
    _yt_title_cache.clear()
    _yt_title_cache.update(titles)
    _yt_title_mtime[0] = newest
    return _yt_title_cache


def _mpv_body(uri):
    return uri[len("mpv:"):] if uri.startswith("mpv:") else uri


# Optional per-track control fragment: "...#dev=<audio-device>" pins mpv's output
# for that track (e.g. "#dev=pulse/am" for a whole-house sink, "#dev=auto" for
# the default device). Lets a single mpv broker be told, per play, where to send
# audio — used by Mopidy-Books to offer "rooms" vs "at desk" picks in Iris.
_DEV_MARKER = "#dev="


def _split_device(s):
    """(clean, device|None) — strip a trailing #dev=<value> control fragment."""
    i = s.rfind(_DEV_MARKER)
    if i != -1:
        return s[:i], s[i + len(_DEV_MARKER):]
    return s, None


def _local_path(uri):
    """Filesystem path for a local-file mpv: URI, else None (yt/http/stream)."""
    body, _dev = _split_device(_mpv_body(uri))
    if body.startswith("file://"):
        body = unquote(urlparse(body).path)
    if body.startswith("/") and os.path.isfile(body):
        return body
    return None


def _title_for(uri):
    """A human-ish name for an mpv: URI, shown until real metadata arrives.

    For a local file we clean up the basename (drop dir + extension, underscores
    to spaces) so even an untagged file reads as a title rather than a path.
    """
    body, _dev = _split_device(_mpv_body(uri))
    vid = _youtube_id(body)
    if vid:
        title = _yt_titles().get(vid)
        if title:
            return title
        return f"YouTube {vid}"
    path = _local_path(uri)
    if path:
        stem = os.path.splitext(os.path.basename(path))[0]
        return stem.replace("_", " ").strip() or body
    return body


# path -> (mtime, {title, artist, album}); avoids re-probing on every lookup().
_tag_cache: dict[str, tuple[float, dict]] = {}


def _read_tags(path):
    """Embedded title/artist/album for a local audio file (mutagen, cached)."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    hit = _tag_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    tags = {}
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(path, easy=True)
        if mf is not None:
            def first(*keys):
                for k in keys:
                    v = mf.get(k)
                    if v:
                        return v[0]
                return None
            tags = {
                "title": first("title"),
                "artist": first("artist", "albumartist", "composer"),
                "album": first("album"),
            }
    except Exception:  # noqa: BLE001 — tags are best-effort
        logger.debug("tag read failed for %s", path, exc_info=True)
    _tag_cache[path] = (mtime, tags)
    return tags


# path -> (mtime, bool): does the file carry embedded cover art?
_cover_cache: dict[str, tuple[float, bool]] = {}


def _has_cover(path):
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return False
    hit = _cover_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    from .http import extract_cover
    present = extract_cover(path)[0] is not None
    _cover_cache[path] = (mtime, present)
    return present


def youtube_to_url(rest):
    """Convert a YouTube-flavoured URI body into a watch URL mpv+yt-dlp can play.

    Handles the bodies you get after stripping the leading ``mpv:``:
        youtube:video:ID / yt:video:ID / youtube:ID / yt:ID  ->  watch?v=ID
    Anything else (an http(s):// or ytdl:// URL, a file path) is returned as-is
    so mpv's ytdl_hook / demuxer handles it directly.

    Note: two backends can't share the ``youtube:`` scheme (Mopidy core raises
    AssertionError), so this is for URIs explicitly routed through ``mpv:``.
    Mopidy-YouTube keeps the bare ``youtube:`` scheme for search/browse.
    """
    for prefix in ("youtube:video:", "yt:video:", "youtube:", "yt:"):
        if rest.startswith(prefix):
            body = rest[len(prefix):]
            if not body:
                return rest
            # agent-media speaks `yt:<full-url>`, not just `yt:<id>` — if the
            # body is already a URL, hand it straight to mpv's yt-dlp.
            if body.startswith(("http://", "https://", "ytdl://")):
                return body
            return _YT_WATCH.format(body)
    return rest


class MpvPlaybackProvider(backend.PlaybackProvider):
    def __init__(self, audio, backend):
        super().__init__(audio, backend)
        self._ipc = None
        self._current_uri = None
        self._reported_state = "stopped"  # mirrors what we last told core
        self._last_title = None           # de-dupe tags_changed emissions
        self._core = None                 # lazily-resolved Core proxy
        self._current_device = None       # per-track audio-device from #dev=
        self._last_pos_ms = 0             # position captured at last pause()
        self._pending_seek_ms = None      # seek to apply once a reload loads

    # -- helpers -----------------------------------------------------------
    def _reset_ipc(self):
        if self._ipc is not None:
            try:
                self._ipc.close()
            except Exception:
                pass
            self._ipc = None

    def _ensure_ipc(self):
        # Reuse the live connection; otherwise (re)connect. The reconnect path is
        # what makes `media music play` survive a mopidy-mpv.service restart: the
        # backend's persistent socket goes dead, is_alive() reports it, and we
        # rebuild from scratch instead of writing into the stale pipe (which used
        # to silently no-op until mopidy.service itself was restarted).
        if self._ipc is not None and self._ipc.is_alive():
            return self._ipc
        self._reset_ipc()
        path = self.backend.config["mpv"]["ipc_socket"]
        ipc = MpvIPC(path, on_event=self._on_mpv_event)
        ipc.connect()
        # mpv must already be running with:
        #   mpv --idle --input-ipc-server=<path> --no-video \
        #       --ao=<sink that feeds your am-music snapfifo>
        # Output routing is mpv's job, NOT Mopidy's audio actor — see notes.
        # Re-arm the observers on every (re)connect:
        # - media-title: live ICY/stream title (radio) for Iris/Android.
        ipc.command("observe_property", 10, "media-title")
        # - metadata: for YouTube, media-title falls back to the bare
        #   "watch?v=ID" until yt-dlp metadata lands; prefer the real title here.
        ipc.command("observe_property", 12, "metadata")
        # - pause: keep Mopidy in sync if something pauses mpv directly.
        ipc.command("observe_property", 11, "pause")
        self._ipc = ipc
        return self._ipc

    def _core_proxy(self):
        # Backends aren't handed a Core ref, but the actor is in the registry.
        # We need it for one thing: forcing core back to PLAYING when mpv is
        # un-paused by *another* IPC client (e.g. agent-media resuming the book
        # channel after speech). Core's audio-event handler only syncs the
        # PAUSED direction (mopidy core actor.py: "temporary fix for #232"), so
        # an external resume would otherwise leave core/Iris stuck on paused.
        if self._core is None:
            try:
                from mopidy.core import Core
                refs = pykka.ActorRegistry.get_by_class(Core)
                if refs:
                    self._core = refs[0].proxy()
            except Exception:
                logger.exception("mpv: could not resolve Core proxy")
        return self._core

    @staticmethod
    def _looks_like_url_fallback(title):
        # mpv's media-title fallback when no real title yet (URL/last path seg).
        if not title:
            return True
        return title.startswith(("watch?v=", "http://", "https://", "ytdl://"))

    def _emit_title(self, title):
        if title and not self._looks_like_url_fallback(title):
            if title != self._last_title:
                self._last_title = title
                audio.AudioListener.send("tags_changed", tags={"title": [title]})

    def _strip_scheme(self, uri):
        # "mpv:https://..." -> "https://..."   "mpv:file:///x" -> "file:///x"
        return uri[len("mpv:"):] if uri.startswith("mpv:") else uri

    # -- PlaybackProvider API ---------------------------------------------
    def translate_uri(self, uri):
        # Return the real media URI mpv should load. After dropping our scheme,
        # rewrite any wrapped YouTube form to a watch URL so mpv's yt-dlp (with
        # ~/.config/yt-dlp/config: web/mweb/tv clients + node EJS solver + bgutil
        # PoT) resolves it.
        #   mpv:youtube:video:dQw4w9WgXcQ -> https://www.youtube.com/watch?v=...
        #   mpv:https://youtu.be/...      -> https://youtu.be/...  (as-is)
        body, _dev = _split_device(self._strip_scheme(uri))
        return youtube_to_url(body)

    def change_track(self, track):
        self._current_uri = self.translate_uri(track.uri)
        # Remember any per-track output pin so play() can apply it pre-loadfile.
        _body, self._current_device = _split_device(self._strip_scheme(track.uri))
        return True

    def play(self):
        # Retry once: if the connection died (mpv/mopidy-mpv restarted) the first
        # attempt raises ConnectionError; _reset_ipc() forces a fresh connect on
        # the second pass so playback recovers without bouncing mopidy.service.
        for attempt in (1, 2):
            ipc = self._ensure_ipc()
            try:
                # Pin this track's output device first (e.g. rooms vs at-desk),
                # so the very first samples land on the chosen sink.
                if self._current_device:
                    try:
                        ipc.set_property("audio-device", self._current_device)
                    except Exception:
                        logger.warning("mpv: set audio-device %s failed",
                                       self._current_device, exc_info=True)
                # loadfile replace = start fresh; mpv starts unpaused.
                self._pending_seek_ms = None  # fresh play: start at 0
                ipc.command("loadfile", self._current_uri, "replace")
                ipc.set_property("pause", False)
                self._reported_state = "playing"
                return True
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning(
                    "mpv play attempt %d failed (%s); reconnecting", attempt, e
                )
                self._reset_ipc()
            except Exception:
                logger.exception("mpv play failed for %s", self._current_uri)
                return False
        logger.error("mpv play gave up after reconnect for %s", self._current_uri)
        return False

    def resume(self):
        ipc = self._ensure_ipc()
        # Self-heal the dead-resume case: if the stream dropped to idle while
        # Mopidy still thinks it's merely paused (network stream timed out, hit
        # EOF, or was stopped by another IPC client), un-pausing an idle mpv is
        # a silent no-op — the "play does nothing after pause" bug. Detect the
        # idle and reload the current URI, seeking back to where we paused.
        try:
            idle = bool(ipc.get_property("idle-active"))
        except Exception:  # noqa: BLE001
            idle = False
        if idle and self._current_uri:
            try:
                if self._current_device:
                    ipc.set_property("audio-device", self._current_device)
                # Seek back to the paused position once the reload lands
                # (file-loaded), so coming back to a long-dropped book/video
                # doesn't restart from 0.
                self._pending_seek_ms = self._last_pos_ms or None
                ipc.command("loadfile", self._current_uri, "replace")
                ipc.set_property("pause", False)
                self._reported_state = "playing"
                return True
            except Exception:  # noqa: BLE001
                logger.warning("mpv resume-reload failed for %s",
                               self._current_uri, exc_info=True)
                self._reset_ipc()
                ipc = self._ensure_ipc()
        ipc.set_property("pause", False)
        self._reported_state = "playing"
        return True

    def pause(self):
        ipc = self._ensure_ipc()
        # Remember where we are so resume() can restore it if the stream has
        # since dropped to idle (see resume()).
        try:
            pos = ipc.get_property("time-pos")
            if pos is not None:
                self._last_pos_ms = int(pos * 1000)
        except Exception:  # noqa: BLE001
            pass
        ipc.set_property("pause", True)
        self._reported_state = "paused"
        return True

    def stop(self):
        if self._ipc:
            try:
                self._ipc.command("stop")
            except Exception:
                logger.exception("mpv stop failed")
        self._reported_state = "stopped"
        return True

    def seek(self, time_position):
        # Mopidy gives milliseconds; mpv seeks in seconds (absolute).
        self._ensure_ipc().command("seek", time_position / 1000.0, "absolute")
        return True

    def get_time_position(self):
        try:
            pos = self._ensure_ipc().get_property("time-pos")  # seconds (float)
            return int((pos or 0) * 1000)
        except Exception:
            return 0

    # -- mpv -> Mopidy event bridge ---------------------------------------
    def _on_mpv_event(self, evt):
        name = evt.get("event")

        if name == "end-file" and evt.get("reason") == "eof":
            # THE load-bearing line: tell Mopidy core the stream ended so the
            # tracklist advances. This reuses GStreamer's signalling path even
            # though no GStreamer is involved. It's an internal-ish API.
            audio.AudioListener.send("reached_end_of_stream")

        elif name == "file-loaded" and self._current_uri:
            # New stream actually started playing -> let core/Iris refresh.
            audio.AudioListener.send("stream_changed", uri=self._current_uri)
            # If this load was a resume-reload (stream had dropped to idle),
            # jump back to where we paused now that the demuxer is ready.
            if self._pending_seek_ms:
                pos_s = self._pending_seek_ms / 1000.0
                self._pending_seek_ms = None
                try:
                    self._ipc.command("seek", pos_s, "absolute")
                except Exception:  # noqa: BLE001
                    logger.debug("mpv resume-seek to %.1fs failed", pos_s,
                                 exc_info=True)

        elif name == "property-change" and evt.get("name") == "media-title":
            # Live ICY/stream title (radio) — but ignore mpv's URL fallback.
            self._emit_title(evt.get("data"))

        elif name == "property-change" and evt.get("name") == "metadata":
            # Full tag dict from yt-dlp/demuxer; pull the real title out. Keys
            # vary by source (title / icy-title / TITLE), so check a few.
            meta = evt.get("data") or {}
            title = (meta.get("title") or meta.get("icy-title")
                     or meta.get("TITLE") or meta.get("Title"))
            self._emit_title(title)

        elif name == "property-change" and evt.get("name") == "pause":
            # mpv-initiated pause/unpause -> reflect into Mopidy's reported state
            # so frontends don't drift. Frontend-initiated pause/resume already
            # go through core, so guard against echoing those back as no-ops.
            paused = evt.get("data")
            if paused is None:
                return
            new_state = "paused" if paused else "playing"
            if new_state != self._reported_state:
                old_state = self._reported_state
                self._reported_state = new_state
                audio.AudioListener.send(
                    "state_changed",
                    old_state=old_state,
                    new_state=new_state,
                    target_state=None,
                )
                # Core's audio-event handler only acts on the ->PAUSED edge, so
                # an external un-pause (another IPC client resumed mpv) leaves
                # core stuck on paused. Drive it back to PLAYING explicitly.
                # Fire-and-forget the proxy call (no .get()) so we don't block
                # the IPC reader thread. The guard above means we only do this
                # for *external* changes — our own resume() already set
                # _reported_state to "playing" before mpv echoes the event.
                if new_state == "playing":
                    core = self._core_proxy()
                    if core is not None:
                        try:
                            core.playback.resume()
                        except Exception:
                            logger.exception("mpv: core resume() failed")


class MpvLibraryProvider(backend.LibraryProvider):
    """Minimal library so frontends can ENQUEUE mpv: URIs.

    Mopidy core validates a URI via library.lookup() before adding it to the
    tracklist — with no lookup, MPD/Iris reject mpv: URIs as "No such song".
    We don't browse or search (Mopidy-YouTube does that); we just confirm any
    mpv: URI resolves to a single playable Track so it can be queued. mpv fills
    in the real title at playback time via tags_changed.
    """

    root_directory = None  # not browsable

    def lookup(self, uri):
        if not uri.startswith("mpv:"):
            return []
        path = _local_path(uri)
        if path:
            t = _read_tags(path)
            kwargs = {"uri": uri, "name": t.get("title") or _title_for(uri)}
            if t.get("artist"):
                kwargs["artists"] = [Artist(name=t["artist"])]
            if t.get("album"):
                kwargs["album"] = Album(name=t["album"])
            return [Track(**kwargs)]
        # Non-local (YouTube/http/stream): mpv fills the title at playback time.
        return [Track(uri=uri, name=_title_for(uri))]

    def get_images(self, uris):
        """Cover art for local mpv: tracks, served by our /mpv/ http app."""
        from urllib.parse import quote
        from mopidy.models import Image
        result = {}
        for uri in uris:
            path = _local_path(uri)
            if path and _has_cover(path):
                result[uri] = [Image(uri="/mpv/cover?path=" + quote(path))]
                continue
            vid = _youtube_id(_split_device(_mpv_body(uri))[0])
            if vid:
                # Deterministic YouTube thumbnail CDN URL. The *browser* (phone/
                # laptop) fetches this from i.ytimg.com — not mel — so mel's
                # YouTube IP block is irrelevant. mqdefault = 320x180, light.
                result[uri] = [Image(
                    uri=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg")]
            else:
                result[uri] = []
        return result

    def get_chapters(self, uris):
        """Chapters / cue points for mpv: URIs, from mpv's own chapter-list.

        Requires Mopidy core with chapter support (the get_chapters API); on
        older cores this method is simply never called. mpv exposes
        container/yt-dlp chapters via `chapter-list`; we map them to the
        Chapter model. For this PoC we report chapters for the URI currently
        loaded in mpv (a fuller impl would probe each URI headless).
        """
        from mopidy.models import Chapter  # core may predate this model

        result = {uri: [] for uri in uris}
        try:
            ipc = self._ensure_ipc()
            loaded = ipc.get_property("path")
            raw = ipc.get_property("chapter-list") or []
        except Exception:
            return result

        chapters = [
            Chapter(start=int((c.get("time") or 0) * 1000), name=c.get("title"))
            for c in raw
        ]
        for uri in uris:
            target = youtube_to_url(uri[len("mpv:"):]) if uri.startswith("mpv:") else uri
            if loaded and (loaded == target or target in loaded):
                result[uri] = chapters
        return result

    def _ensure_ipc(self):
        # Reuse the playback provider's health-checked connection (reconnects if
        # the mpv socket died); only fall back to a throwaway one if there's no
        # playback provider for some reason.
        pb = self.backend.playback
        if pb is not None:
            return pb._ensure_ipc()
        ipc = MpvIPC(self.backend.config["mpv"]["ipc_socket"])
        ipc.connect()
        return ipc

    def refresh(self, uri=None):
        pass


class MpvBackend(pykka.ThreadingActor, backend.Backend):
    uri_schemes = ["mpv"]

    def __init__(self, config, audio):
        super().__init__()
        self.config = config
        self.audio = audio
        self.playback = MpvPlaybackProvider(audio=audio, backend=self)
        self.library = MpvLibraryProvider(backend=self)
        # No playlists provider — search/browse stays with Mopidy-YouTube et al.

    def on_stop(self):
        if self.playback._ipc:
            self.playback._ipc.close()
