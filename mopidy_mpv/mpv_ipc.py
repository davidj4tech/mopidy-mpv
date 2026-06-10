"""Thin JSON-IPC client for an mpv instance started with --input-ipc-server.

This is the same control channel you already drive for the book channel
(sink-book.sock + mpvc). We talk to it directly so Mopidy can be the only
thing issuing commands.

Protocol: newline-delimited JSON over a unix socket.
  - request:  {"command": ["set_property", "pause", true], "request_id": 7}
  - reply:    {"request_id": 7, "error": "success", "data": ...}
  - event:    {"event": "end-file", "reason": "eof"}
"""

import json
import socket
import threading
import logging

logger = logging.getLogger(__name__)


class MpvIPC:
    def __init__(self, socket_path, on_event=None):
        self._path = socket_path
        self._on_event = on_event or (lambda evt: None)
        self._sock = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._pending = {}            # request_id -> threading.Event/result box
        self._reader = None
        self._running = False

    # -- lifecycle ---------------------------------------------------------
    def connect(self):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self._path)
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # -- command/response ---------------------------------------------------
    def is_alive(self):
        """True while the socket is connected and the reader is running. Goes
        False once the peer (mpv) closes — i.e. mopidy-mpv.service restarted —
        so callers can reconnect instead of writing into a broken pipe."""
        return self._running and self._sock is not None

    def command(self, *args, timeout=2.0):
        """Send a command and block for its reply. Returns reply 'data'.

        Raises ConnectionError if the link is down (or drops mid-command) so the
        caller can rebuild the connection; TimeoutError if mpv just never replies.
        """
        with self._lock:
            if not self._running:
                raise ConnectionError("mpv ipc not connected")
            self._req_id += 1
            rid = self._req_id
            box = {"event": threading.Event(), "reply": None}
            self._pending[rid] = box
            payload = json.dumps({"command": list(args), "request_id": rid})
            try:
                self._sock.sendall(payload.encode() + b"\n")
            except OSError as e:
                self._pending.pop(rid, None)
                self._running = False
                raise ConnectionError(f"mpv ipc send failed: {e}")

        if not box["event"].wait(timeout):
            self._pending.pop(rid, None)
            raise TimeoutError(f"mpv command timed out: {args}")
        # The reader thread fires every pending event when the socket dies, so a
        # set event with the link already down means "disconnected", not a real
        # reply of None.
        if not self._running:
            raise ConnectionError("mpv ipc closed during command")
        return box["reply"]

    def set_property(self, name, value):
        return self.command("set_property", name, value)

    def get_property(self, name):
        return self.command("get_property", name)

    # -- read loop ----------------------------------------------------------
    def _read_loop(self):
        buf = b""
        try:
            while self._running:
                try:
                    chunk = self._sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        logger.warning("mpv: bad JSON line: %r", line)
                        continue
                    self._dispatch(msg)
        finally:
            # Socket closed (mpv exited / mopidy-mpv.service restarted). Mark the
            # link dead so the next _ensure_ipc() reconnects, and release any
            # in-flight waiters so they fail fast instead of blocking the timeout.
            self._running = False
            self._fail_pending()

    def _fail_pending(self):
        with self._lock:
            boxes = list(self._pending.values())
            self._pending.clear()
        for box in boxes:
            box["event"].set()   # reply stays None; command() sees _running False

    def _dispatch(self, msg):
        if "request_id" in msg:
            box = self._pending.pop(msg["request_id"], None)
            if box is not None:
                box["reply"] = msg.get("data")
                box["event"].set()
        elif "event" in msg:
            try:
                self._on_event(msg)
            except Exception:
                logger.exception("mpv event handler blew up")
