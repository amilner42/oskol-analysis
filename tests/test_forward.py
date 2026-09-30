"""Forwarding: what UPSTREAM_URL does, and what it must never do.

The engine is moving onto a desktop reached over a tailnet, and this app
becomes the thing in front of it. Two properties matter more than the rest:
a review's answer must arrive byte for byte (Oskol stores it verbatim and
reads it in Gleam), and an unreachable desktop must become an answer the
caller can act on rather than an exception nobody catches.

No tailnet and no engine here: the upstream is a local HTTP server.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app.main import app


class _Upstream(BaseHTTPRequestHandler):
    """Records what it was asked and answers what it was told to."""

    status = 200
    payload = {"ok": True, "from": "upstream"}
    seen: list[dict] = []

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        _Upstream.seen.append(
            # Header names are case-insensitive on the wire and BaseHTTPRequestHandler
            # keeps whatever case the sender used, so normalise once here rather
            # than making every assertion guess.
            {"path": self.path, "method": self.command, "body": body,
             "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        out = json.dumps(_Upstream.payload).encode()
        self.send_response(_Upstream.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *a):  # keep pytest's output readable
        pass


@pytest.fixture
def upstream():
    server = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Upstream.seen = []
    _Upstream.status = 200
    _Upstream.payload = {"ok": True, "from": "upstream"}
    yield server, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def forwarding(upstream, monkeypatch):
    _, url = upstream
    monkeypatch.setenv("UPSTREAM_URL", url)
    monkeypatch.delenv("UPSTREAM_PROXY", raising=False)
    return TestClient(app)


def test_unset_is_the_app_it_always_was(monkeypatch):
    """No UPSTREAM_URL, no forwarding: /health is answered here, by the engine."""
    monkeypatch.delenv("UPSTREAM_URL", raising=False)
    body = TestClient(app).get("/health").json()
    assert body["engine"] == "bgsage"
    assert "review_workers" in body


def test_health_is_forwarded(forwarding, upstream):
    """The status page asks 'is analysis working'. That answer is upstream's."""
    server, _ = upstream
    body = forwarding.get("/health").json()
    assert body == {"ok": True, "from": "upstream"}
    assert _Upstream.seen[-1]["path"] == "/health"


def test_health_self_is_never_forwarded(forwarding):
    """Fly's platform check reads this container. A sleeping desktop must not
    make Fly restart a machine that is fine."""
    body = forwarding.get("/health/self").json()
    assert body["ok"] is True
    assert body["upstream"] is not None
    assert _Upstream.seen == []


def test_a_review_goes_through_unchanged(forwarding):
    """Oskol stores the engine's answer verbatim and reads it in Gleam, so a
    body that changes shape in transit is a corrupted review."""
    _Upstream.payload = {"turns": [{"index": 0}], "timing_ms": 1234, "levels": {"move": "4ply"}}
    request = {"jacoby": True, "turns": [{"player": 0, "board": [0] * 26}]}

    answer = forwarding.post("/backgammon/review", json=request)

    assert answer.json() == _Upstream.payload
    sent = _Upstream.seen[-1]
    assert sent["path"] == "/backgammon/review"
    assert sent["method"] == "POST"
    assert json.loads(sent["body"]) == request


def test_upstream_status_is_relayed_not_flattened(forwarding):
    """A 422 from the engine means the caller's encoder is wrong, and it has
    to arrive as a 422 or the caller learns the wrong thing."""
    _Upstream.status = 422
    _Upstream.payload = {"detail": "turns[3]: bad"}

    answer = forwarding.post("/backgammon/review", json={})

    assert answer.status_code == 422
    assert answer.json() == {"detail": "turns[3]: bad"}


def test_an_unreachable_upstream_is_a_502(monkeypatch):
    """A desktop that is asleep is the expected failure, not the exceptional
    one. It must be an answer: Oskol's queue stores a failure and its sweep
    retries, which it cannot do with a hang or a stack trace."""
    monkeypatch.setenv("UPSTREAM_URL", "http://127.0.0.1:9")  # discard
    monkeypatch.delenv("UPSTREAM_PROXY", raising=False)
    monkeypatch.setenv("UPSTREAM_CONNECT_TIMEOUT_S", "2")

    import importlib

    from app import forward

    importlib.reload(forward)
    try:
        answer = TestClient(app).post("/backgammon/review", json={})
        assert answer.status_code == 502
        assert "upstream" in answer.json()["detail"]
    finally:
        monkeypatch.delenv("UPSTREAM_CONNECT_TIMEOUT_S", raising=False)
        importlib.reload(forward)


def test_query_and_content_type_survive(forwarding):
    forwarding.post("/backgammon/review?trace=1", json={"a": 1})
    sent = _Upstream.seen[-1]
    assert "trace=1" in sent["path"]
    assert sent["headers"]["content-type"] == "application/json"


def test_hop_by_hop_headers_are_not_relayed(forwarding):
    """Host and the connection-scoped headers belong to one hop; passing them
    on is how a proxy confuses the next one."""
    forwarding.get("/health", headers={"connection": "keep-alive", "te": "trailers"})
    sent = _Upstream.seen[-1]["headers"]
    assert "te" not in sent
    # Host is not relayed but rebuilt for the hop actually being made, which is
    # what stops the next server seeing a name meant for this one.
    assert sent["host"].startswith("127.0.0.1")
