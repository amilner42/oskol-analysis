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
from dataclasses import asdict
from typing import Literal

import bgsage
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

LEVELS = (
    "1ply", "2ply", "3ply", "4ply",
    "truncated1", "truncated2", "truncated3",
    "rollout",
)
Level = Literal[LEVELS]
Owner = Literal["centered", "player", "opponent"]

app = FastAPI(title="oskol-analysis", version="0.1.0")

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
    level: Level = "2ply"

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
    pass


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


@app.get("/health")
def health() -> dict:
    return {"ok": True, "engine": "bgsage", "model": bgsage.PRODUCTION_MODEL, "levels": LEVELS}


@app.post("/moves")
def moves(req: MovesRequest) -> dict:
    """Every legal play for the dice, best first, with equities and probabilities."""
    return _moves(req)


@app.post("/cube")
def cube(req: CubeRequest) -> dict:
    """The pre-roll cube decision: no double, double/take, double/pass equities."""
    return _cube(req)


@app.post("/position")
def position(req: PositionRequest) -> dict:
    """A post-move position, before the opponent rolls."""
    return _position(req)


_KINDS = {
    "moves": (MovesRequest, _moves),
    "cube": (CubeRequest, _cube),
    "position": (PositionRequest, _position),
}


@app.post("/batch")
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
