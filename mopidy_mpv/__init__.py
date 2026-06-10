import pathlib

from mopidy import config, ext

__version__ = "0.1.0"


class Extension(ext.Extension):
    dist_name = "Mopidy-Mpv"
    ext_name = "mpv"
    version = __version__

    def get_default_config(self):
        return config.read(pathlib.Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        schema["ipc_socket"] = config.Path()
        schema["mpv_command"] = config.String(optional=True)
        return schema

    def setup(self, registry):
        from .backend import MpvBackend
        from .mixer import MpvMixer

        registry.add("backend", MpvBackend)
        registry.add("mixer", MpvMixer)
