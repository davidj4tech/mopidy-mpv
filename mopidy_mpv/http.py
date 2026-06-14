"""Tiny HTTP app that serves embedded cover art for local mpv: tracks.

Mopidy mounts this under /mpv/ on the HTTP server, so the library provider's
get_images() can hand frontends (Iris) a fetchable URL — /mpv/cover?path=... —
that returns the JPEG/PNG embedded in the audio file. Without this, web clients
have no way to load artwork for files we play through the mpv: scheme (we don't
use Mopidy-Local, which is what normally serves local images).
"""

import logging
import os
from urllib.parse import unquote

import tornado.web

logger = logging.getLogger(__name__)

_AUDIO_EXT = (".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".oga", ".flac")


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


def mpv_http_factory(config, core):
    # Mounted at /mpv/ -> route is /mpv/cover.
    return [(r"/cover", CoverHandler)]
