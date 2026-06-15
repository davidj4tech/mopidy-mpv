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
import subprocess
from urllib.parse import unquote

import tornado.web

logger = logging.getLogger(__name__)

_AUDIO_EXT = (".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".oga", ".flac")

_COOKIES_DIR = os.path.expanduser("~/.config/yt-dlp/cookies")
_YT_PROFILE = os.path.expanduser("~/.local/bin/yt-profile")
_ACTIVE_BOOK = os.path.join(_COOKIES_DIR, "active-book.txt")
_NAME_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")


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
            self.write({"current": self._current()})
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


def mpv_http_factory(config, core):
    # Mounted at /mpv/ -> routes are /mpv/cover and /mpv/ytbook.
    return [(r"/cover", CoverHandler), (r"/ytbook", BookProfileHandler)]
