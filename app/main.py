"""oskol-analysis: a thin HTTP face on the Open Sage backgammon engine.

Every request carries a position in Open Sage's board format, always from
the perspective of the player on roll:

  board[1..24]  points, counted from the on-roll player's side: their
                checkers are positive, the opponent's negative; the on-roll
                player moves from 24 down to 1 and bears off past 1
  board[25]     the on-roll player's checkers on the bar (nonnegative count)
  board[0]      the opponent's checkers on the bar (nonnegative count)
  borne-off checkers are not stored (15 minus that side's interior and bar)

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
from typing import Literal

import bgsage
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from app import pool
from app.review import (CUBE_LEVEL, LEVELS, MOVE_LEVEL, Level, ReviewRequest,
                        analyze_serially, review_game)

Owner = Literal["centered", "player", "opponent"]

app = FastAPI(title="oskol-analysis", version="0.1.0")
# Routes are namespaced by game; backgammon is the only one so far.
backgammon = APIRouter(prefix="/backgammon", tags=["backgammon"])

_analyzers: dict[str, bgsage.BgBotAnalyzer] = {}
_lock = threading.Lock()
_batch_pool = ThreadPoolExecutor(max_workers=int(os.getenv("BATCH_WORKERS", "4")))


def analyzer(level: str) -> bgsage.BgBotAnalyzer:
    """One engine per level, created on first use and shared by every request."""
    with _lock:
        if level not in _analyzers:
            _analyzers[level] = bgsage.create_analyzer(level)
        return _analyzers[level]


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
        if board[25] < 0 or board[0] < 0:
            raise ValueError("board[25] and board[0] are nonnegative bar counts")
        mine = board[25] + sum(n for n in board[1:25] if n > 0)
        theirs = board[0] - sum(n for n in board[1:25] if n < 0)
        if mine > 15 or theirs > 15:
            raise ValueError("more than 15 checkers for one side")
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


def _moves(req: MovesRequest) -> dict:
    result = analyzer(req.level).checker_play(
        req.board, req.dice[0], req.dice[1],
        include_game_plans=req.include_game_plans,
        **req.match_kwargs(),
    )
    return asdict(result)


def _cube(req: CubeRequest) -> dict:
    return asdict(analyzer(req.level).cube_action(req.board, **req.match_kwargs()))


def _position(req: PositionRequest) -> dict:
    return asdict(analyzer(req.level).post_move_analytics(req.board, **req.match_kwargs()))


@backgammon.post("/review")
def review(req: ReviewRequest) -> dict:
    """Grade a whole game: every cube decision, every move, the luck, totals.

    Defaults to 4-ply moves and cubes, the turns spread over a process pool
    (app.pool). The response echoes the `levels` used and the `timing_ms`.
    """
    analyze = pool.analyze_in_parallel if pool.workers() > 1 else analyze_serially(analyzer)
    try:
        return review_game(req, analyze)
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from e
    except BrokenProcessPool as e:
        raise HTTPException(503, detail="an analysis worker died; try again") from e


@app.get("/health")
def health() -> dict:
    return {"ok": True, "engine": "bgsage", "model": bgsage.PRODUCTION_MODEL, "levels": LEVELS,
            "review_workers": pool.workers(), "engine_threads": pool.engine_threads()}


@backgammon.post("/moves")
def moves(req: MovesRequest) -> dict:
    """Every legal play for the dice, best first, with equities and probabilities."""
    return _moves(req)


@backgammon.post("/cube")
def cube(req: CubeRequest) -> dict:
    """The pre-roll cube decision: no double, double/take, double/pass equities."""
    return _cube(req)


@backgammon.post("/position")
def position(req: PositionRequest) -> dict:
    """A post-move position, before the opponent rolls."""
    return _position(req)


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
    """
    jobs = []
    for i, item in enumerate(req.items):
        model, run = _KINDS[item.kind]
        try:
            parsed = model.model_validate(item.request)
        except ValueError as e:
            raise HTTPException(422, detail=f"items[{i}]: {e}") from e
        jobs.append((run, parsed))
    results = list(_batch_pool.map(lambda job: job[0](job[1]), jobs))
    return {"results": results}


app.include_router(backgammon)
