"""A game's turns analysed in parallel, one engine per worker process.

bgsage evaluates one position at a time per call; its own threads split a
single evaluation but a review is dozens of independent ones, and at 4-ply
each takes seconds. So a review fans its turns out over a process pool sized
to the machine: every worker loads its engines once (per level, on first use)
and keeps them. Results come back in turn order, and every turn's analysis
depends only on that turn (bgsage clears its cache after each evaluation), so
a parallel review returns exactly what a serial one does.

A review carrying ONE turn is the opposite shape, and the pool is the wrong
answer to it: there is nothing to spread, so the turn lands on a single worker
holding one twentieth of the machine while the other nineteen sit idle. On the
production i9 one 4-ply turn takes 11.7 s at one thread and 2.5 s at twenty,
so such a request skips the pool entirely and runs in the server process on an
engine given SOLO_ENGINE_THREADS (app.main.solo_analyzer). Both callers that
send lone turns -- Oskol grading a turn while the game is still going, and the
bot reviewing every move -- are waiting on the answer. The many-turn review is
untouched: those workers are already saturated and widening them would only
oversubscribe.

Knobs (env):
  REVIEW_WORKERS         worker processes (default: the machine's cores)
  REVIEW_ENGINE_THREADS  bgsage threads per worker for a multi-ply evaluation
                         (default: cores / workers; bgsage uses at least 2)
  SOLO_ENGINE_THREADS    bgsage threads for work that arrives on its own and is
                         run in the server process rather than in the pool
                         (default: the machine's cores)
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import bgsage

from app.review import ReviewRequest, Turn, analyze_turn

CORES = os.cpu_count() or 1


def workers() -> int:
    return max(1, int(os.getenv("REVIEW_WORKERS", CORES)))


def engine_threads() -> int:
    return max(1, int(os.getenv("REVIEW_ENGINE_THREADS", CORES // workers() or 1)))


def solo_threads() -> int:
    """Threads for an evaluation that has the server process to itself.

    All of them: nothing else is queued behind it, and bgsage's threads split
    one evaluation, which is exactly what a lone request is. Past the physical
    cores the returns stop (the i9's 2.5 s at twenty threads is ten cores'
    worth of work), but asking for the logical count costs nothing and saves
    the machine having to be told how it is built.
    """
    return max(1, int(os.getenv("SOLO_ENGINE_THREADS", CORES)))


# --- in the worker processes -------------------------------------------------

_engines: dict[str, bgsage.BgBotAnalyzer] = {}
_threads = 0


def _init(threads: int) -> None:
    global _threads
    _threads = threads


def _engine(level: str) -> bgsage.BgBotAnalyzer:
    if level not in _engines:
        _engines[level] = bgsage.create_analyzer(level, parallel_threads=_threads)
    return _engines[level]


def _turn(job: tuple[int, Turn, ReviewRequest]) -> dict:
    index, turn, req = job
    return analyze_turn(index, turn, req, _engine)


# --- in the server -------------------------------------------------------------

_pool: ProcessPoolExecutor | None = None
_lock = threading.Lock()


def pool() -> ProcessPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            # spawn, not fork: the server has threads (uvicorn's, bgsage's), and
            # forking a threaded process can inherit a held lock.
            _pool = ProcessPoolExecutor(
                max_workers=workers(),
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init,
                initargs=(engine_threads(),),
            )
        return _pool


def _reset(broken: ProcessPoolExecutor) -> None:
    global _pool
    with _lock:
        if _pool is broken:
            _pool = None
    broken.shutdown(wait=False, cancel_futures=True)


def analyze_in_parallel(req: ReviewRequest) -> list[dict]:
    """analyze_turn over every turn across the pool, in turn order.

    A worker that dies (out of memory, say) breaks the pool; the next review
    gets a fresh one and this one raises.
    """
    executor = pool()
    # The request rides with every job: a few KB, far below a turn's engine time.
    settings = req.model_copy(update={"turns": []})
    jobs = [(i, t, settings) for i, t in enumerate(req.turns)]
    futures = [executor.submit(_turn, job) for job in jobs]
    try:
        return [f.result() for f in futures]
    except BrokenProcessPool:
        _reset(executor)
        raise
    except BaseException:
        for f in futures:
            f.cancel()
        raise
