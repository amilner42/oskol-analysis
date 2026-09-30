"""oskol-analysis: a thin HTTP face on the Open Sage backgammon engine.

Every request carries a position in Open Sage's board format, always from
the perspective of the player on roll:

  board[1..24]  points, counted from the on-roll player's side: their
                checkers are positive, the opponent's negative; the on-roll
                player moves from 24 down to 1 and bears off past 1
  board[25]     the on-roll player's checkers on the bar (positive)
  board[0]      the opponent's checkers on the bar (negative)
  borne-off checkers are not stored (15 minus what is on the board)

Cube owner is "centered", "player" (the on-roll player) or "opponent".
away1/away2 are match scores as points still needed by the on-roll player
and the opponent; both 0 means a money game.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from typing import Callable, Literal

import bgsage
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from app import forward, pool
from app.review import (CUBE_LEVEL, LEVELS, MOVE_LEVEL, Level, ReviewRequest,
                        analyze_serially, review_game)

Owner = Literal["centered", "player", "opponent"]

app = FastAPI(title="oskol-analysis", version="0.1.0")


@app.middleware("http")
async def _upstream(request: Request, call_next):
    """Answer with the machine UPSTREAM_URL names, when it names one.

    See app/forward.py. Unset, this costs one environment lookup a request
    and changes nothing.
    """
    to = forward.upstream()
    if to is None or not forward.forwarding() or request.url.path == "/health/self":
        return await call_next(request)
    return await forward.forward(request, to)
# Routes are namespaced by game; backgammon is the only one so far.
backgammon = APIRouter(prefix="/backgammon", tags=["backgammon"])

_analyzers: dict[tuple[str, int | None], bgsage.BgBotAnalyzer] = {}
_lock = threading.Lock()
_batch_pool = ThreadPoolExecutor(max_workers=int(os.getenv("BATCH_WORKERS", "4")))

# What a route hands the analysis code to get an engine for a level.
Engines = Callable[[str], bgsage.BgBotAnalyzer]


def analyzer(level: str, threads: int | None = None) -> bgsage.BgBotAnalyzer:
    """One engine per level and thread count, created on first use and shared.

    How many threads an engine splits an evaluation over is fixed when it is
    created, so a wider engine is another entry here and never a change to an
    existing one: whatever a caller already holds keeps the threads it was
    made with. `threads=None` leaves the count to bgsage, as this has always
    done. The engines are the resident memory of this process, so they are
    made lazily and only in the combinations something actually asks for.
    """
    with _lock:
        key = (level, threads)
        if key not in _analyzers:
            kwargs = {} if threads is None else {"parallel_threads": threads}
            _analyzers[key] = bgsage.create_analyzer(level, **kwargs)
        return _analyzers[key]


def solo_analyzer(level: str) -> bgsage.BgBotAnalyzer:
    """The engine for an evaluation that has this process to itself: all of it.

    bgsage's own threads split one evaluation, which is the only thing a lone
    request has. Two such requests overlapping do oversubscribe and slow each
    other down, and that is the trade taken knowingly: this server answers a
    few latency-sensitive callers rather than a crowd, and half a wide engine
    still beats by a long way the one thread a pool worker would have had.
    """
    return analyzer(level, pool.solo_threads())


class Position(BaseModel):
    board: list[int] = Field(min_length=26, max_length=26)
    cube_value: int = 1
    cube_owner: Owner = "centered"
    away1: int = 0
    away2: int = 0
    is_crawford: bool = False
    jacoby: bool = True
    level: Level = MOVE_LEVEL

    @field_validator("board")
    @classmethod
    def _sane_board(cls, board: list[int]) -> list[int]:
        mine = sum(n for n in board if n > 0)
        theirs = -sum(n for n in board if n < 0)
        if mine > 15 or theirs > 15:
            raise ValueError("more than 15 checkers for one side")
        if board[25] < 0 or board[0] > 0:
            raise ValueError("board[25] is the on-roll bar (>= 0), board[0] the opponent's (<= 0)")
        return board

    @field_validator("cube_value")
    @classmethod
    def _cube_power_of_two(cls, value: int) -> int:
        if value < 1 or value & (value - 1):
            raise ValueError("cube_value must be a power of two")
        return value

    def match_kwargs(self) -> dict:
        return dict(
            cube_value=self.cube_value,
            cube_owner=self.cube_owner,
            away1=self.away1,
            away2=self.away2,
            is_crawford=self.is_crawford,
            jacoby=self.jacoby,
        )


class MovesRequest(Position):
    dice: tuple[int, int]
    include_game_plans: bool = False

    @field_validator("dice")
    @classmethod
    def _dice_faces(cls, dice: tuple[int, int]) -> tuple[int, int]:
        if not all(1 <= d <= 6 for d in dice):
            raise ValueError("dice must be 1..6")
        return dice


class CubeRequest(Position):
    level: Level = CUBE_LEVEL


class PositionRequest(Position):
    """A post-move position: evaluated for the player who just moved."""


class BatchItem(BaseModel):
    kind: Literal["moves", "cube", "position"]
    request: dict


class BatchRequest(BaseModel):
    items: list[BatchItem] = Field(max_length=500)


def _moves(req: MovesRequest, engines: Engines) -> dict:
    result = engines(req.level).checker_play(
        req.board, req.dice[0], req.dice[1],
        include_game_plans=req.include_game_plans,
        **req.match_kwargs(),
    )
    return asdict(result)


def _cube(req: CubeRequest, engines: Engines) -> dict:
    return asdict(engines(req.level).cube_action(req.board, **req.match_kwargs()))


def _position(req: PositionRequest, engines: Engines) -> dict:
    return asdict(engines(req.level).post_move_analytics(req.board, **req.match_kwargs()))


@backgammon.post("/review")
def review(req: ReviewRequest) -> dict:
    """Grade a whole game: every cube decision, every move, the luck, totals.

    Defaults to 4-ply moves and cubes, the turns spread over a process pool
    (app.pool). The response echoes the `levels` used and the `timing_ms`.

    A review of a single turn has nothing to spread, so it is analysed here
    instead, on an engine holding the whole machine -- see app.pool's header
    for the measurements, and solo_analyzer above. Two callers send lone
    turns and both are waiting on the reply. Running here also means such a
    request never starts a pool it has no work for. The cut is at one turn
    because one turn is what those callers send; where widening stops paying
    (two turns? four?) has not been measured, so it is not guessed at here.

    REVIEW_WORKERS=1 takes the same path for a whole game: the turns go one
    at a time through this process, so there is nothing to share a core with
    and no reason for that one turn at a time to be narrow either.
    """
    lone = len(req.turns) == 1
    analyze = (pool.analyze_in_parallel if not lone and pool.workers() > 1
               else analyze_serially(solo_analyzer))
    try:
        return review_game(req, analyze)
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from e
    except BrokenProcessPool as e:
        raise HTTPException(503, detail="an analysis worker died; try again") from e


@app.get("/health")
async def health(response: Response) -> dict:
    """Is analysis working?

    Forwarding, this never runs: the answer anyone wants is the desktop's and
    the middleware has already gone and got it. Before that, with UPSTREAM_URL
    set but the work still here, it asks the desktop anyway and answers 503
    when it cannot be reached — so Oskol's status page tells the truth about a
    tailnet that nothing depends on yet, which is the only order in which
    learning it is flaky is cheap.
    """
    body = {"ok": True, "engine": "bgsage", "model": bgsage.PRODUCTION_MODEL, "levels": LEVELS,
            "review_workers": pool.workers(), "engine_threads": pool.engine_threads(),
            "solo_engine_threads": pool.solo_threads()}

    to = forward.upstream()
    if to is not None:
        reached, detail = await forward.probe(to)
        body["upstream"] = {"url": to, "reached": reached, "detail": detail,
                            "forwarding": forward.forwarding()}
        if not reached:
            body["ok"] = False
            response.status_code = 503

    return body


@app.get("/health/self")
def health_self() -> dict:
    """Is *this* process up? Never forwarded, so Fly's platform check reads
    this container and not a desktop that may be asleep — restarting this
    machine cannot fix that, and a check that says otherwise loops."""
    return {"ok": True, "upstream": forward.upstream(),
            "forwarding": forward.forwarding()}


@backgammon.post("/moves")
def moves(req: MovesRequest) -> dict:
    """Every legal play for the dice, best first, with equities and probabilities."""
    return _moves(req, solo_analyzer)


@backgammon.post("/cube")
def cube(req: CubeRequest) -> dict:
    """The pre-roll cube decision: no double, double/take, double/pass equities."""
    return _cube(req, solo_analyzer)


@backgammon.post("/position")
def position(req: PositionRequest) -> dict:
    """A post-move position, before the opponent rolls."""
    return _position(req, solo_analyzer)


_KINDS = {
    "moves": (MovesRequest, _moves),
    "cube": (CubeRequest, _cube),
    "position": (PositionRequest, _position),
}


@backgammon.post("/batch")
def batch(req: BatchRequest) -> dict:
    """Several evaluations in one round trip, answered in order.

    Items are validated up front so a bad one fails the whole batch with a
    422 before any engine time is spent.

    A batch of one is a lone evaluation like any other and gets the machine.
    Several already run concurrently over _batch_pool, so wide engines there
    would fight each other for the same cores -- the pool's own argument --
    and they keep bgsage's default thread count, as they always have.
    """
    jobs = []
    for i, item in enumerate(req.items):
        model, run = _KINDS[item.kind]
        try:
            parsed = model.model_validate(item.request)
        except ValueError as e:
            raise HTTPException(422, detail=f"items[{i}]: {e}") from e
        jobs.append((run, parsed))
    engines = solo_analyzer if len(jobs) == 1 else analyzer
    results = list(_batch_pool.map(lambda job: job[0](job[1], engines), jobs))
    return {"results": results}


app.include_router(backgammon)
