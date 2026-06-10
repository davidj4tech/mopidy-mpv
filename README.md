# Mopidy-Mpv

A [Mopidy](https://mopidy.com/) backend that plays URIs through an **external
mpv** process over mpv's JSON-IPC socket, bypassing GStreamer entirely.

It handles the `mpv:` URI scheme: a `PlaybackProvider` that drives mpv via IPC
plus a *minimal* `LibraryProvider` (just enough `lookup()` for frontends to
enqueue `mpv:` URIs — it does not browse or search). Pair it with
Mopidy-YouTube / Mopidy-Stream for discovery and route the things you want mpv
to play through the `mpv:` scheme.

## Why

mpv + yt-dlp is far more robust at playing YouTube (and other ytdl sources)
than Mopidy's GStreamer pipeline — especially behind PO-token / signature
challenges. Letting mpv own resolution and decoding, while Mopidy stays the
queue/now-playing brain, sidesteps the GStreamer breakage.

YouTube-flavoured bodies are normalised to a watch URL so mpv's `ytdl_hook`
resolves them:

```
mpv:youtube:video:ID  ->  https://www.youtube.com/watch?v=ID
mpv:yt:<full-url>      ->  <full-url>          (handed straight to ytdl_hook)
mpv:https://…          ->  https://…           (as-is)
mpv:file:///…          ->  file:///…           (as-is)
```

## How it expects to run

mpv is managed **externally** (systemd/runit), started idle with an IPC socket
and an audio device — Mopidy only connects to it:

```
mpv --idle=yes --no-video --no-terminal \
    --input-ipc-server=$XDG_RUNTIME_DIR/mopidy-mpv.sock \
    --ao=pulse --audio-device=pulse/<your-sink>
```

The backend holds a persistent IPC connection but **reconnects on failure**: if
the mpv process restarts at runtime, the next play re-establishes the socket
(re-arming property observers) instead of silently no-op'ing — so you never
have to bounce Mopidy to recover.

## Config

```ini
[mpv]
enabled = true
ipc_socket = $XDG_RUNTIME_DIR/mopidy-mpv.sock
mpv_command =          # optional; blank = mpv is managed externally
```

## Installation

### Requirements

- **Mopidy** ≥ 3.4 and **Python** ≥ 3.9 (`pykka` ≥ 4.0 comes in as a dependency).
- **mpv** installed and on `PATH`, managed externally (see
  [How it expects to run](#how-it-expects-to-run)).
- For YouTube/yt-dlp sources: **yt-dlp** available to mpv's `ytdl_hook`, and a
  **JavaScript runtime** (deno or node) for signature/n-challenge solving.

### Install the extension

Not on PyPI yet — install from the repository.

From GitHub:

```
pip install git+https://github.com/davidj4tech/mopidy-mpv.git
```

Or for development, clone and install editable:

```
git clone https://github.com/davidj4tech/mopidy-mpv.git
cd mopidy-mpv
pip install -e .
```

Install it into the **same environment as Mopidy** so it's discovered via the
`mopidy.ext` entry point. Verify it loaded:

```
mopidy deps | grep -i mpv     # should list Mopidy-Mpv
```

### Enable & configure

Add the `[mpv]` section to your `mopidy.conf` (see [Config](#config)), point
`ipc_socket` at the socket your externally-managed mpv exposes, then start mpv
([How it expects to run](#how-it-expects-to-run)) and restart Mopidy.

## Status

Working PoC. Provides backend + mixer; no playlists/browse provider by design.
