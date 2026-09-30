"""Answering with a machine that is not this one.

Set ``UPSTREAM_URL`` and this process stops computing: every request is
passed through to that address and its answer returned verbatim, status and
body unchanged. Unset it and nothing here runs at all — the app is exactly
what it was.

That switch is the point. The engine is moving onto a desktop reached over a
tailnet (Aveline ticket ``bg-analysis-imac``), and the way back from that is
``fly secrets unset UPSTREAM_URL`` and a bigger machine, not a different
deploy of different code. One environment variable is cheaper to reason
about at 2am than two images.

Two things are deliberately not forwarded:

``/health/self`` answers here, always, as long as this process is up. Fly's
own health check reads it. If the platform's check went through to the
desktop, a desktop that is asleep would read as a sick machine and Fly would
restart this one in a loop over a condition restarting cannot fix.

``/health`` *is* forwarded, which is the other half of the same thought.
Oskol's status page reads it to answer "is analysis working", and the honest
answer to that is the desktop's, not ours. A proxy that reports its own
health while the thing behind it is dead is worse than no status page.

Tailscale runs here in userspace networking, so there is no kernel route to
the tailnet: connections have to go out through tailscaled's own proxy,
which is what ``UPSTREAM_PROXY`` names.
"""

from __future__ import annotations

import os

import httpx
from fastapi import Request, Response

# A review of a long match at 4-ply is minutes of work, and Oskol's client
# waits twenty of them. Anything shorter here would turn a slow answer into
# a failed one somewhere the caller cannot see.
READ_TIMEOUT_S = float(os.environ.get("UPSTREAM_READ_TIMEOUT_S", 20 * 60))
CONNECT_TIMEOUT_S = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT_S", 15))

# Hop-by-hop headers belong to one connection and must not be relayed.
_DROP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def upstream() -> str | None:
    """The address to answer with, or None to answer here."""
    return (os.environ.get("UPSTREAM_URL") or "").rstrip("/") or None


def _proxy() -> str | None:
    return os.environ.get("UPSTREAM_PROXY") or None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=_proxy(),
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    )


async def forward(request: Request, to: str) -> Response:
    """Pass one request through and hand back what came of it.

    A failure to reach upstream is a 502 naming what went wrong, never an
    exception out of the middleware: the caller gets an answer it can store
    and retry from, and Oskol's queue already knows what to do with one.
    """
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP}
    body = await request.body()

    try:
        async with _client() as client:
            answer = await client.request(
                request.method,
                f"{to}{request.url.path}",
                params=request.query_params,
                headers=headers,
                content=body,
            )
    except httpx.HTTPError as e:
        # The class name carries the useful part (ConnectTimeout, ConnectError,
        # ReadTimeout) where str(e) is often empty.
        return Response(
            content=f'{{"detail":"upstream {type(e).__name__}: {e}"}}',
            status_code=502,
            media_type="application/json",
        )

    return Response(
        content=answer.content,
        status_code=answer.status_code,
        headers={k: v for k, v in answer.headers.items() if k.lower() not in _DROP},
        media_type=answer.headers.get("content-type"),
    )
