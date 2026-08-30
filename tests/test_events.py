import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from xangi_stackchan.events import (
    XangiEventStream,
    iter_sse_messages,
    normalize_xangi_stream_url,
)


def test_normalize_base_url():
    assert (
        normalize_xangi_stream_url("http://127.0.0.1:18890")
        == "http://127.0.0.1:18890/api/events/stream"
    )


def test_normalize_stream_url():
    assert (
        normalize_xangi_stream_url("http://127.0.0.1:18890/api/events/stream")
        == "http://127.0.0.1:18890/api/events/stream"
    )


def test_normalize_rejects_empty():
    with pytest.raises(ValueError):
        normalize_xangi_stream_url("")


def test_sse_heartbeat_comment_is_forwarded(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_lines(self, decode_unicode=False):
            assert decode_unicode is True
            return iter([": keepalive", ""])

    monkeypatch.setattr(
        "xangi_stackchan.events.requests.get", lambda *args, **kwargs: Response()
    )

    assert list(iter_sse_messages("http://127.0.0.1/events")) == [
        {"event": "heartbeat", "data": "{}"}
    ]


def test_interruptible_stream_closes_without_waiting_for_sse_traffic():
    connected = threading.Event()
    release = threading.Event()

    class SilentSseHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            connected.set()
            release.wait(5)

    server = ThreadingHTTPServer(("127.0.0.1", 0), SilentSseHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stream = XangiEventStream(
        f"http://127.0.0.1:{server.server_port}/events", timeout=65
    )
    worker = threading.Thread(target=lambda: list(stream), daemon=True)
    worker.start()
    try:
        assert connected.wait(2)
        started = time.monotonic()
        stream.close()
        worker.join(1)
        assert not worker.is_alive()
        assert time.monotonic() - started < 1
    finally:
        release.set()
        server.shutdown()
        server.server_close()
