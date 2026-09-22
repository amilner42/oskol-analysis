"""Whole-game review: every decision graded the way a player reads a review.

The caller replays its own game and sends one entry per turn, each from the
perspective of the player on roll (see app.main for the board format). For
every turn this returns the cube decision (was a double legal, taken,
correct?), the checker play (the move made, the best move, the top few, the
equity lost) and the luck of the roll, plus per-player totals.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from typing import Callable, Iterable, Literal

import bgsage
from bgsage.text_export import compute_move_notation
from pydantic import BaseModel, Field, model_validator

LEVELS = (
    "1ply", "2ply", "3ply", "4ply",
    "truncated1", "truncated2", "truncated3",
    "rollout",
)
Level = Literal[LEVELS]

MOVE_LEVEL = "2ply"   # /moves and /cube: the quick standard setting
CUBE_LEVEL = "3ply"
# A review grades every decision at 4-ply: XG-grade numbers, run in parallel
# across the machine's cores (app.pool).
REVIEW_MOVE_LEVEL = "4ply"
REVIEW_CUBE_LEVEL = "4ply"
# Luck reads the per-roll equities of a cube analysis run with
# incl_2ply_details. bgsage 2.0.20260907 corrupts a 4-ply cube analysis when
# asked for those details (ND/DT drift by ~0.1 and change with the thread
# count), so luck gets its own analysis, never deeper than 3-ply (2-ply luck),
# and the graded cube analysis never asks for details at 4-ply.
LUCK_LEVEL = "3ply"
_PLY = {"1ply": 1, "2ply": 2, "3ply": 3, "4ply": 4}

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
    move_level: Level = REVIEW_MOVE_LEVEL
    cube_level: Level = REVIEW_CUBE_LEVEL
    top_moves: int = Field(default=5, ge=1, le=50)
    include_luck: bool = True
    # Every legal play's board and equity loss, not just the top few. Compact
    # (no notation, no probabilities) so a whole game stays small enough to
    # store: a caller that must grade any legal answer needs them all.
    all_results: bool = False


def can_double(turn: Turn) -> bool:
    if turn.is_crawford or turn.cube_owner == "opponent":
        return False
    money = turn.away1 == 0 and turn.away2 == 0
    # A cube that already covers what the doubler needs is dead.
    return money or turn.cube_value < turn.away1


def play_cube(turn: Turn) -> tuple[int, str]:
    """The cube the checker play of this turn is made on: its value and owner.

    A double that was taken turns the cube before the mover rolls: it is worth
    twice as much and it belongs to the taker, who from the mover's side is
    the opponent. The cube *decision* is still graded on the pre-offer cube --
    that is what the doubler was looking at -- and only what follows the take
    moves on. No double, or a double that was passed, leaves the cube alone.
    """
    if turn.doubled and turn.response == "take":
        return turn.cube_value * 2, "opponent"
    return turn.cube_value, turn.cube_owner


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


def review_move(turn: Turn, result: bgsage.CheckerPlayResult, top_n: int,
                all_results: bool = False) -> dict:
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
    out = {
        "played": move_entry(turn, played, played_rank + 1),
        "best": move_entry(turn, moves[0], 1),
        "top": top,
        "n_legal": len(moves),
        "forced": forced,
        "error": error,
        "grade": "best" if error == 0.0 else grade(error),   # a tie lost nothing
        "eval_level": result.eval_level,
    }
    if all_results:
        # Every legal play, in rank order, board and equity loss only: the
        # engine evaluated them all anyway, and a caller grading an answer
        # that did not make the top few needs its number from stored data.
        out["results"] = [{"board": m.board, "equity_diff": m.equity_diff} for m in moves]
    return out


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


def luck_level(cube_level: str) -> str | None:
    """The cube analysis luck is read from, or None when there is no luck to be had.

    Luck needs per-roll equities, which a 1-ply cube analysis does not carry.
    Up to 3-ply they come from the graded cube analysis itself; deeper, from a
    separate 3-ply one (see LUCK_LEVEL).
    """
    ply = _PLY.get(cube_level)
    if ply == 1:
        return None
    if ply is None or ply > _PLY[LUCK_LEVEL]:
        return LUCK_LEVEL
    return cube_level


def levels(req: ReviewRequest) -> dict:
    return {"move": req.move_level, "cube": req.cube_level,
            "luck": luck_level(req.cube_level) if req.include_luck else None}


def validate(req: ReviewRequest) -> None:
    """Every check that needs no evaluation, before any engine time is spent.

    Raises ValueError naming the first bad turn, so a caller's encoder bug
    422s at once instead of after minutes of 4-ply analysis.
    """
    for index, turn in enumerate(req.turns):
        if turn.doubled and not can_double(turn):
            raise ValueError(f"turns[{index}]: a double was not legal here")
        if turn.dice is None:
            continue
        legal = bgsage.possible_moves(turn.board, *turn.dice)
        if not legal:
            if turn.played is not None and turn.played != turn.board:
                raise ValueError(f"turns[{index}]: no legal move, but a move was played")
        elif turn.played is None:
            raise ValueError(f"turns[{index}]: dice were rolled but no move was played")
        elif turn.played not in legal:
            raise ValueError(f"turns[{index}]: played board is not a legal move for these dice")


def analyze_turn(index: int, turn: Turn, req: ReviewRequest, analyzer) -> dict:
    """One turn's engine work: its cube decision, its luck, its move.

    Depends on nothing but the turn and the request, so turns can be analysed
    in any order, on any process, and still come out the same.
    """
    # The cube as the turn opened, before any double was offered: what the
    # cube decision (and the luck that shares its analysis) is judged on. The
    # checker play below moves on to what a take left (play_cube).
    match = dict(cube_value=turn.cube_value, cube_owner=turn.cube_owner,
                 away1=turn.away1, away2=turn.away2,
                 is_crawford=turn.is_crawford, jacoby=req.jacoby)
    # The cube everything after the double is played and judged on. It is the
    # turn's own cube unless a double was taken, and then it is twice that,
    # owned by the taker.
    cube_value, cube_owner = play_cube(turn)
    play = {**match, "cube_value": cube_value, "cube_owner": cube_owner}
    post_take = play != match
    out: dict = {"index": index, "player": turn.player, "dice": turn.dice,
                 "cube": None, "move": None, "luck": None}

    lucky = luck_level(req.cube_level) if req.include_luck and turn.dice is not None else None
    cube_analysis = luck_analysis = None
    if can_double(turn):
        # A post-take turn rolls on a cube the graded analysis knows nothing
        # about, so luck cannot ride along on it: it gets its own analysis.
        shared = lucky == req.cube_level and not post_take
        cube_analysis = analyzer(req.cube_level).cube_action(
            turn.board, incl_2ply_details=shared, **match)
        out["cube"] = review_cube(turn, cube_analysis)
        if shared:
            luck_analysis = cube_analysis
    if lucky and luck_analysis is None:
        luck_analysis = analyzer(lucky).cube_action(turn.board, incl_2ply_details=True, **play)

    if turn.dice is not None:
        d1, d2 = turn.dice
        if luck_analysis is not None:
            luck = bgsage.roll_luck(luck_analysis, d1, d2, is_opening_roll=index == 0)
            if luck is not None:
                out["luck"] = {"luck": luck.luck, "actual_equity": luck.actual_equity,
                               "average_equity": luck.average_equity,
                               "level_label": luck.level_label}
        if not bgsage.possible_moves(turn.board, d1, d2):
            # Nothing was evaluated, so `results` (when asked for) is empty
            # rather than missing: every move carries it or none does.
            out["move"] = {"danced": True, "n_legal": 0}
            if req.all_results:
                out["move"]["results"] = []
        else:
            # The checker play happens after any double was answered, so it is
            # made on the cube the take left behind, not the pre-offer one.
            result = analyzer(req.move_level).checker_play(turn.board, d1, d2, **play)
            try:
                out["move"] = review_move(turn, result, req.top_moves, req.all_results)
            except ValueError as e:
                raise ValueError(f"turns[{index}]: {e}") from e
    return out


Mapper = Callable[[ReviewRequest], Iterable[dict]]


def analyze_serially(analyzer) -> Mapper:
    return lambda req: (analyze_turn(i, t, req, analyzer) for i, t in enumerate(req.turns))


def review_game(req: ReviewRequest, analyze: Mapper) -> dict:
    """Grade a game. `analyze` runs analyze_turn over every turn and yields the
    results in turn order (serially here, or across processes: app.pool)."""
    started = time.monotonic()
    validate(req)
    turns_out = list(analyze(req))

    totals = {0: new_totals(), 1: new_totals()}
    for out in turns_out:
        me, them = totals[out["player"]], totals[1 - out["player"]]
        if out["cube"]:
            me["cube"]["decisions"] += 1
            me["cube"]["error"] += out["cube"]["doubler"]["error"]
            count(me["cube"]["mistakes"], out["cube"]["doubler"]["mistake"])
            if out["cube"]["taker"]:
                them["cube"]["decisions"] += 1
                them["cube"]["error"] += out["cube"]["taker"]["error"]
                count(them["cube"]["mistakes"], out["cube"]["taker"]["mistake"])
        if out["luck"]:
            me["luck"] += out["luck"]["luck"]
        m = out["move"]
        if m and not m.get("danced"):
            if m["forced"]:
                me["moves"]["forced"] += 1
            else:
                me["moves"]["decisions"] += 1
                me["moves"]["error"] += m["error"]
                count(me["moves"]["grades"], m["grade"])

    for t in totals.values():
        t["error"] = t["moves"]["error"] + t["cube"]["error"]
        decisions = t["moves"]["decisions"] + t["cube"]["decisions"]
        # XG's Performance Rating: equity lost per unforced decision, times 500.
        t["pr"] = t["error"] / decisions * 500 if decisions else 0.0

    return {
        "levels": levels(req),
        "turns": turns_out,
        "players": [totals[0], totals[1]],
        "timing_ms": round((time.monotonic() - started) * 1000),
    }
