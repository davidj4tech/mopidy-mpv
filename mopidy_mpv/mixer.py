"""Mopidy mixer that proxies volume/mute to the same mpv instance.

Why a separate actor: Iris/Android/MPD volume sliders call Mopidy core's
*mixer*, never the playback backend. So a backend alone gives you working
transport but dead volume sliders. This closes that gap.

mpv's --input-ipc-server accepts multiple simultaneous clients, so the mixer
opens its own connection to the same socket the backend uses.

To activate, the user sets in mopidy.conf:
    [audio]
    mixer = mpv
"""

import logging

import pykka

from mopidy import mixer

from .mpv_ipc import MpvIPC

logger = logging.getLogger(__name__)


class MpvMixer(pykka.ThreadingActor, mixer.Mixer):
    name = "mpv"

    def __init__(self, config):
        super().__init__()
        self._socket_path = config["mpv"]["ipc_socket"]
        self._ipc = None
        self._known_volume = None
        self._known_mute = None

    def on_start(self):
        self._ipc = MpvIPC(self._socket_path, on_event=self._on_mpv_event)
        try:
            self._ipc.connect()
            # Ask mpv to push us volume/mute changes (e.g. if something else
            # nudges mpv) so Mopidy's reported volume stays truthful.
            self._ipc.command("observe_property", 1, "volume")
            self._ipc.command("observe_property", 2, "mute")
        except Exception:
            logger.exception("MpvMixer could not connect to %s", self._socket_path)

    def on_stop(self):
        if self._ipc:
            self._ipc.close()

    # -- Mopidy Mixer API --------------------------------------------------
    def get_volume(self):
        try:
            vol = self._ipc.get_property("volume")  # mpv: 0..100 (float)
            return int(round(vol)) if vol is not None else None
        except Exception:
            return self._known_volume

    def set_volume(self, volume):
        # volume: int 0..100 from Mopidy core.
        try:
            self._ipc.set_property("volume", float(volume))
            self._known_volume = volume
            mixer.MixerListener.send("volume_changed", volume=volume)
            return True
        except Exception:
            logger.exception("MpvMixer set_volume failed")
            return False

    def get_mute(self):
        try:
            return bool(self._ipc.get_property("mute"))
        except Exception:
            return self._known_mute

    def set_mute(self, mute):
        try:
            self._ipc.set_property("mute", bool(mute))
            self._known_mute = mute
            mixer.MixerListener.send("mute_changed", mute=bool(mute))
            return True
        except Exception:
            logger.exception("MpvMixer set_mute failed")
            return False

    # -- mpv -> Mopidy: keep sliders honest if mpv changes underneath us ---
    def _on_mpv_event(self, evt):
        if evt.get("event") != "property-change":
            return
        if evt.get("name") == "volume" and evt.get("data") is not None:
            vol = int(round(evt["data"]))
            if vol != self._known_volume:
                self._known_volume = vol
                mixer.MixerListener.send("volume_changed", volume=vol)
        elif evt.get("name") == "mute" and evt.get("data") is not None:
            m = bool(evt["data"])
            if m != self._known_mute:
                self._known_mute = m
                mixer.MixerListener.send("mute_changed", mute=m)
