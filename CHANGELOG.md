# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-10

Initial release.

### Added
- `MpvBackend` for the `mpv:` URI scheme: a `PlaybackProvider` that drives an
  externally-managed mpv process over its JSON-IPC socket, bypassing GStreamer.
- YouTube-flavoured URI normalisation (`youtube:video:ID` / `yt:ID` /
  `yt:<full-url>` → watch URL) so mpv's `ytdl_hook` resolves them.
- Minimal `LibraryProvider` (lookup-only) so frontends can enqueue `mpv:` URIs;
  pairs with Mopidy-YouTube / Mopidy-Stream for discovery. Also exposes
  `get_chapters` from mpv's `chapter-list` where the core supports it.
- `MpvMixer` for volume control over the same IPC channel.
- mpv → Mopidy event bridge: end-of-stream advances the tracklist, and
  media-title / metadata / pause changes are reflected back to the core.
- Reconnect-on-failure in the IPC client and backend: `MpvIPC.is_alive()`, the
  reader thread marks the link dead and releases in-flight waiters on
  disconnect, `command()` raises `ConnectionError` on a down/dropped link,
  `_ensure_ipc()` reconnects (re-arming property observers), and `play()`
  retries once — so a runtime mpv restart recovers without bouncing Mopidy.

[Unreleased]: https://github.com/davidj4tech/mopidy-mpv/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/davidj4tech/mopidy-mpv/releases/tag/v0.1.0
