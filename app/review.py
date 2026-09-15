"""Whole-game review: every decision graded the way a player reads a review.

The caller replays its own game and sends one entry per turn, each from the
perspective of the player on roll (see app.main for the board format). For
every turn this returns the cube decision (was a double legal, taken,
correct?), the checker play (the move made, the best move, the top few, the
equity lost) and the luck of the roll, plus per-player totals.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

import bgsage
from bgsage.text_export import compute_move_notation
from pydantic import BaseModel, Field, model_validator

MOVE_LEVEL = "2ply"   # the standard review setting: what error rates are quoted at
CUBE_LEVEL = "3ply"

# Equity lost, in the doubler's or mover's units. XG's bands.
GRADES = ((0.16, "very_bad"), (0.08, "bad"), (0.02, "doubtful"))


def grade(error: float) -> str:
    for threshold, label in GRADES:
        if error >= threshold:
            return label
    return "ok"


class Turn(BaseModel):
    player: Literal[0, 1]
    board: list[int] = Field(min_length=26, max_length=26)
    cube_value: int = 1
    cube_owner: Literal["centered", "player", "opponent"] = "centered"
    away1: int = 0
    away2: int = 0
    is_crawford: bool = False
    doubled: bool = False
    response: Literal["take", "pass"] | None = None
    dice: tuple[int, int] | None = None
    played: list[int] | None = Field(default=None, min_length=26, max_length=26)

    @model_validator(mode="after")
    def _consistent(self) -> "Turn":
        if self.response is not None and not self.doubled:
            raise ValueError("a response needs a double")
        if self.doubled and self.response is None:
            raise ValueError("a double needs a response")
        if self.response == "pass" and (self.dice or self.played):
            raise ValueError("a passed double ends the turn: no dice, no move")
        if self.dice is not None and not all(1 <= d <= 6 for d in self.dice):
            raise ValueError("dice must be 1..6")
        if self.played is not None and self.dice is None:
            raise ValueError("a move needs dice")
        return self


class ReviewRequest(BaseModel):
    turns: list[Turn] = Field(min_length=1, max_length=1000)
    jacoby: bool = True
    move_level: str = MOVE_LEVEL
    cube_level: str = CUBE_LEVEL
    top_moves: int = Field(default=5, ge=1, le=50)
    include_luck: bool = True


def can_double(turn: Turn) -> bool:
    if turn.is_crawford or turn.cube_owner == "opponent":
        return False
    money = turn.away1 == 0 and turn.away2 == 0
    # A cube that already covers what the doubler needs is dead.
    return money or turn.cube_value < turn.away1


def review_cube(turn: Turn, analysis: bgsage.CubeActionResult) -> dict:
    """Grade the doubler and, if there was a double, the responder."""
    nd, dt, dp = analysis.equity_nd, analysis.equity_dt, analysis.equity_dp
    doubled_value = min(dt, dp)          # the opponent answers to the doubler's cost
    optimal = max(nd, doubled_value)
    taken = doubled_value if turn.doubled else nd
    doubler_error = max(0.0, optimal - taken)
    doubler_mistake = None
    if doubler_error > 0:
        doubler_mistake = "wrong_double" if turn.doubled else "missed_double"

    taker_error = 0.0
    taker_mistake = None
    if turn.doubled:
        chosen = dt if turn.response == "take" else dp
        taker_error = max(0.0, chosen - doubled_value)
        if taker_error > 0:
            taker_mistake = "wrong_take" if turn.response == "take" else "wrong_pass"

    return {
        "action": "double" if turn.doubled else "no_double",
        "response": turn.response,
        "analysis": {
            "equity_nd": nd, "equity_dt": dt, "equity_dp": dp,
            "optimal_action": analysis.optimal_action,
            "should_double": analysis.should_double,
            "should_take": analysis.should_take,
            "cubeless_equity": analysis.cubeless_equity,
            "probs": asdict(analysis.probs),
            "eval_level": analysis.eval_level,
        },
        "doubler": {"error": doubler_error, "grade": grade(doubler_error), "mistake": doubler_mistake},
        "taker": None if not turn.doubled else
            {"error": taker_error, "grade": grade(taker_error), "mistake": taker_mistake},
    }


def move_entry(turn: Turn, m: bgsage.MoveAnalysis, rank: int) -> dict:
    d1, d2 = turn.dice
    return {
        "rank": rank,
        "notation": compute_move_notation(turn.board, m.board, d1, d2),
        "board": m.board,
        "equity": m.equity,
        "cubeless_equity": m.cubeless_equity,
        "equity_diff": m.equity_diff,
        "probs": asdict(m.probs),
    }


def review_move(turn: Turn, result: bgsage.CheckerPlayResult, top_n: int) -> dict:
    """The move made against every legal one. Raises ValueError if it is not legal."""
    moves = result.moves
    played_rank = next((i for i, m in enumerate(moves) if m.board == turn.played), None)
    if played_rank is None:
        raise ValueError("played board is not a legal move for these dice")
    played = moves[played_rank]
    forced = len(moves) == 1
    # equity_diff is best-relative and negative for worse plays; error is the loss.
    error = 0.0 if forced else max(0.0, -played.equity_diff)
    top = [move_entry(turn, m, i + 1) for i, m in enumerate(moves[:top_n])]
    if played_rank >= top_n:
        top.append(move_entry(turn, played, played_rank + 1))
    return {
        "played": move_entry(turn, played, played_rank + 1),
        "best": move_entry(turn, moves[0], 1),
        "top": top,
        "n_legal": len(moves),
        "forced": forced,
        "error": error,
        "grade": "best" if error == 0.0 else grade(error),   # a tie lost nothing
        "eval_level": result.eval_level,
    }


def new_totals() -> dict:
    return {
        "moves": {"decisions": 0, "forced": 0, "error": 0.0, "grades": {}},
        "cube": {"decisions": 0, "error": 0.0, "mistakes": {}},
        "luck": 0.0,
        "error": 0.0,
        "pr": 0.0,
    }


def count(bucket: dict, key: str | None) -> None:
    if key is not None:
        bucket[key] = bucket.get(key, 0) + 1


def review_game(req: ReviewRequest, analyzer) -> dict:
    move_engine = analyzer(req.move_level)
    cube_engine = analyzer(req.cube_level)
    totals = {0: new_totals(), 1: new_totals()}
    turns_out = []
    # Luck comes from the cube analysis's per-roll equities, which need 2-ply or deeper.
    want_luck = req.include_luck and req.cube_level != "1ply"

    for index, turn in enumerate(req.turns):
        match = dict(cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                     away1=turn.away1, away2=turn.away2,
                     is_crawford=turn.is_crawford, jacoby=req.jacoby)
        me, them = totals[turn.player], totals[1 - turn.player]
        out: dict = {"index": index, "player": turn.player, "dice": turn.dice,
                     "cube": None, "move": None, "luck": None}

        legal_double = can_double(turn)
        cube_analysis = None
        if legal_double or want_luck:
            cube_analysis = cube_engine.cube_action(
                turn.board, incl_2ply_details=want_luck, **match)
        if legal_double:
            out["cube"] = review_cube(turn, cube_analysis)
            me["cube"]["decisions"] += 1
            me["cube"]["error"] += out["cube"]["doubler"]["error"]
            count(me["cube"]["mistakes"], out["cube"]["doubler"]["mistake"])
            if out["cube"]["taker"]:
                them["cube"]["decisions"] += 1
                them["cube"]["error"] += out["cube"]["taker"]["error"]
                count(them["cube"]["mistakes"], out["cube"]["taker"]["mistake"])
        elif turn.doubled:
            raise ValueError(f"turns[{index}]: a double was not legal here")

        if turn.dice is not None:
            d1, d2 = turn.dice
            if want_luck and cube_analysis is not None:
                luck = bgsage.roll_luck(cube_analysis, d1, d2, is_opening_roll=index == 0)
                if luck is not None:
                    out["luck"] = {"luck": luck.luck, "actual_equity": luck.actual_equity,
                                   "average_equity": luck.average_equity,
                                   "level_label": luck.level_label}
                    me["luck"] += luck.luck
            legal = bgsage.possible_moves(turn.board, d1, d2)
            if not legal:
                if turn.played is not None and turn.played != turn.board:
                    raise ValueError(f"turns[{index}]: no legal move, but a move was played")
                out["move"] = {"danced": True, "n_legal": 0}
            else:
                if turn.played is None:
                    raise ValueError(f"turns[{index}]: dice were rolled but no move was played")
                result = move_engine.checker_play(turn.board, d1, d2, **match)
                try:
                    out["move"] = review_move(turn, result, req.top_moves)
                except ValueError as e:
                    raise ValueError(f"turns[{index}]: {e}") from e
                m = out["move"]
                if m["forced"]:
                    me["moves"]["forced"] += 1
                else:
                    me["moves"]["decisions"] += 1
                    me["moves"]["error"] += m["error"]
                    count(me["moves"]["grades"], m["grade"])
        turns_out.append(out)

    for t in totals.values():
        t["error"] = t["moves"]["error"] + t["cube"]["error"]
        decisions = t["moves"]["decisions"] + t["cube"]["decisions"]
        # XG's Performance Rating: equity lost per unforced decision, times 500.
        t["pr"] = t["error"] / decisions * 500 if decisions else 0.0

    return {
        "levels": {"moves": req.move_level, "cube": req.cube_level},
        "turns": turns_out,
        "players": [totals[0], totals[1]],
    }
