import random

import bgsage
from fastapi.testclient import TestClient

from app import pool
from app.main import analyzer, app
from app.review import ReviewRequest, analyze_serially, review_game

client = TestClient(app)
START = bgsage.STARTING_BOARD


def self_play(seed: int, max_turns: int = 80) -> list[dict]:
    """A plausible game: 1-ply best moves, no cube. Boards are always on-roll view."""
    rng = random.Random(seed)
    engine = bgsage.create_analyzer("1ply")
    board, player, turns = list(START), 0, []
    for _ in range(max_turns):
        d1, d2 = rng.randint(1, 6), rng.randint(1, 6)
        legal = bgsage.possible_moves(board, d1, d2)
        played = engine.checker_play(board, d1, d2).moves[0].board if legal else None
        turns.append({"player": player, "board": board, "dice": [d1, d2], "played": played})
        board = bgsage.flip_board(played if played else board)
        player = 1 - player
        if bgsage.check_game_over(board):
            break
    return turns


def test_review_of_a_short_game():
    turns = self_play(7, max_turns=8)
    r = client.post("/backgammon/review", json={
        "turns": turns, "move_level": "1ply", "cube_level": "2ply", "top_moves": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["levels"] == {"move": "1ply", "cube": "2ply", "luck": "2ply"}
    assert isinstance(body["timing_ms"], int) and body["timing_ms"] >= 0
    assert len(body["turns"]) == len(turns)
    first = body["turns"][0]
    assert first["cube"]["action"] == "no_double"
    assert first["cube"]["doubler"]["mistake"] in (None, "missed_double")
    assert first["luck"] is not None
    move = first["move"]
    assert move["played"]["notation"]
    assert move["best"]["rank"] == 1
    assert len(move["top"]) <= 4
    # A 1-ply best play reviewed at 1-ply is the best play.
    assert move["grade"] == "best" and move["error"] == 0.0
    for p in body["players"]:
        assert p["moves"]["decisions"] + p["moves"]["forced"] >= 1
        assert p["pr"] >= 0


def test_a_blunder_is_graded():
    worst = bgsage.create_analyzer("1ply").checker_play(START, 3, 1).moves[-1]
    turns = [{"player": 0, "board": START, "dice": [3, 1], "played": worst.board}]
    r = client.post("/backgammon/review", json={"turns": turns, "move_level": "1ply", "cube_level": "1ply"})
    body = r.json()
    move = body["turns"][0]["move"]
    assert move["played"]["rank"] == move["n_legal"]
    assert move["error"] > 0 and move["grade"] != "best"
    assert move["top"][-1]["rank"] == move["n_legal"]   # the played move is appended
    assert body["players"][0]["moves"]["error"] == move["error"]


def test_cube_decisions_are_attributed():
    # Player 0 doubles at the start; player 1 takes and rolls.
    turns = [
        {"player": 0, "board": START, "doubled": True, "response": "take",
         "dice": [3, 1], "played": bgsage.create_analyzer("1ply").checker_play(START, 3, 1).moves[0].board},
    ]
    r = client.post("/backgammon/review", json={"turns": turns, "move_level": "1ply", "cube_level": "1ply"})
    assert r.status_code == 200, r.text
    cube = r.json()["turns"][0]["cube"]
    assert cube["action"] == "double" and cube["response"] == "take"
    assert cube["doubler"]["mistake"] == "wrong_double"
    assert cube["taker"]["mistake"] is None
    players = r.json()["players"]
    assert players[0]["cube"]["decisions"] == 1 and players[1]["cube"]["decisions"] == 1


def test_illegal_played_board_is_422():
    turns = self_play(1, max_turns=1)
    turns[0]["played"] = START
    r = client.post("/backgammon/review", json={"turns": turns, "move_level": "1ply", "cube_level": "1ply"})
    assert r.status_code == 422
    assert "turns[0]" in r.json()["detail"]


def test_crawford_has_no_cube():
    turns = self_play(2, max_turns=1)
    turns[0].update({"away1": 1, "away2": 3, "is_crawford": True})
    r = client.post("/backgammon/review", json={"turns": turns, "move_level": "1ply", "cube_level": "1ply"})
    assert r.json()["turns"][0]["cube"] is None


def test_parallel_review_matches_serial():
    req = ReviewRequest.model_validate({
        "turns": self_play(11, max_turns=16), "move_level": "1ply", "cube_level": "2ply"})
    serial = review_game(req, analyze_serially(analyzer))
    parallel = review_game(req, pool.analyze_in_parallel)
    serial.pop("timing_ms"), parallel.pop("timing_ms")
    assert [t["index"] for t in parallel["turns"]] == list(range(len(req.turns)))
    assert parallel == serial


def test_review_defaults_to_4ply():
    req = ReviewRequest.model_validate({"turns": self_play(1, max_turns=1)})
    assert (req.move_level, req.cube_level, req.top_moves) == ("4ply", "4ply", 5)


class Recorder:
    """A fake engine factory: records the cube calls, answers from a 1-ply engine."""

    def __init__(self):
        self.calls = []
        self.engine = bgsage.create_analyzer("1ply")

    def __call__(self, level):
        outer = self

        class Engine:
            def cube_action(self, board, incl_2ply_details=False, **kw):
                outer.calls.append((level, incl_2ply_details))
                # Luck needs per-roll details, which a 1-ply analysis lacks: borrow 2-ply's.
                engine = analyzer("2ply") if incl_2ply_details else outer.engine
                return engine.cube_action(board, incl_2ply_details=incl_2ply_details, **kw)

            def checker_play(self, *a, **kw):
                return outer.engine.checker_play(*a, **kw)

        return Engine()


def test_4ply_cube_never_asks_for_details():
    # bgsage's 4-ply cube analysis is wrong when asked for per-roll details:
    # the graded analysis goes without, and luck comes from a 3-ply one.
    turns = self_play(4, max_turns=2)
    rec = Recorder()
    body = review_game(ReviewRequest.model_validate({"turns": turns}), analyze_serially(rec))
    assert rec.calls == [("4ply", False), ("3ply", True)] * 2
    assert body["levels"] == {"move": "4ply", "cube": "4ply", "luck": "3ply"}
    assert body["turns"][0]["luck"] is not None

    rec = Recorder()
    review_game(ReviewRequest.model_validate({"turns": turns, "cube_level": "3ply"}),
                analyze_serially(rec))
    assert rec.calls == [("3ply", True)] * 2      # one analysis serves both


def test_levels_are_echoed_and_checked():
    turns = self_play(3, max_turns=2)
    r = client.post("/backgammon/review", json={
        "turns": turns, "move_level": "1ply", "cube_level": "1ply", "include_luck": False})
    assert r.json()["levels"] == {"move": "1ply", "cube": "1ply", "luck": None}
    r = client.post("/backgammon/review", json={"turns": turns, "move_level": "9ply"})
    assert r.status_code == 422


class Spy:
    """A fake engine factory that records what each call was asked to evaluate.

    Answers from a 1-ply engine so the numbers are real but cheap; the point
    of the tests below is the cube context each call was given.
    """

    def __init__(self):
        self.cube_calls, self.play_calls = [], []
        self.engine = bgsage.create_analyzer("1ply")

    def __call__(self, level):
        outer = self

        class Engine:
            def cube_action(self, board, incl_2ply_details=False, **kw):
                outer.cube_calls.append(kw)
                engine = analyzer("2ply") if incl_2ply_details else outer.engine
                return engine.cube_action(board, incl_2ply_details=incl_2ply_details, **kw)

            def checker_play(self, board, d1, d2, **kw):
                outer.play_calls.append(kw)
                return outer.engine.checker_play(board, d1, d2, **kw)

        return Engine()


def cube_context(call: dict) -> tuple[int, str]:
    return call["cube_value"], call["cube_owner"]


def spy_on(turns: list[dict], **overrides) -> Spy:
    """Review these turns at 1-ply with no luck, and keep the calls made."""
    spy = Spy()
    req = ReviewRequest.model_validate(
        {"turns": turns, "move_level": "1ply", "cube_level": "1ply",
         "include_luck": False, **overrides})
    spy.body = review_game(req, analyze_serially(spy))
    return spy


def doubled_turn(player: int = 0, response: str = "take", **fields) -> dict:
    """A turn that opens with a double: the mover doubles, then rolls 3-1."""
    turn = {"player": player, "board": list(START), "doubled": True, "response": response}
    if response == "take":
        turn |= {"dice": [3, 1],
                 "played": bgsage.create_analyzer("1ply").checker_play(START, 3, 1).moves[0].board}
    return turn | fields


def test_checker_play_after_a_take_is_on_the_doubled_cube():
    # The cube decision is the one the doubler faced: centered, worth 1. The
    # move that follows is played on a 2-cube the taker owns.
    spy = spy_on([doubled_turn()])
    assert [cube_context(c) for c in spy.cube_calls] == [(1, "centered")]
    assert [cube_context(c) for c in spy.play_calls] == [(2, "opponent")]


def test_a_redouble_taken_hands_the_doubled_cube_back():
    # The mover already owned a 2-cube, redoubled to 4 and was taken: from
    # here the 4-cube is the opponent's.
    spy = spy_on([doubled_turn(cube_value=2, cube_owner="player")])
    assert [cube_context(c) for c in spy.cube_calls] == [(2, "player")]
    assert [cube_context(c) for c in spy.play_calls] == [(4, "opponent")]


def test_both_orientations_see_the_same_post_take_cube():
    # Boards and cube owner are always relative to the player on roll, so who
    # is doubling (player 0 or 1) changes nothing about the context.
    for player in (0, 1):
        spy = spy_on([doubled_turn(player=player)])
        assert [cube_context(c) for c in spy.play_calls] == [(2, "opponent")]
        assert spy.body["turns"][0]["player"] == player


def test_a_take_at_a_match_score_keeps_the_score_and_doubles_the_cube():
    spy = spy_on([doubled_turn(away1=5, away2=3)])
    cube, play = spy.cube_calls[0], spy.play_calls[0]
    assert cube_context(cube) == (1, "centered") and cube_context(play) == (2, "opponent")
    for call in (cube, play):
        assert (call["away1"], call["away2"], call["is_crawford"]) == (5, 3, False)


def test_jacoby_is_passed_through_to_the_post_take_play():
    # Jacoby is the caller's money-game rule and it travels unchanged; bgsage
    # itself only applies it to a centered cube, so the post-take evaluation
    # is the same either way. Asserting both keeps that from silently drifting.
    for jacoby in (True, False):
        spy = spy_on([doubled_turn()], jacoby=jacoby)
        assert cube_context(spy.play_calls[0]) == (2, "opponent")
        assert spy.cube_calls[0]["jacoby"] is jacoby
        assert spy.play_calls[0]["jacoby"] is jacoby
    engine = bgsage.create_analyzer("1ply")
    on, off = (engine.checker_play(START, 3, 1, cube_value=2, cube_owner="opponent",
                                   jacoby=j).moves for j in (True, False))
    assert [(m.board, m.equity) for m in on] == [(m.board, m.equity) for m in off]


def test_no_double_and_a_passed_double_leave_the_cube_where_it_was():
    plain = self_play(5, max_turns=1)
    plain[0] |= {"cube_value": 2, "cube_owner": "player"}
    spy = spy_on(plain)
    assert cube_context(spy.cube_calls[0]) == (2, "player")
    assert cube_context(spy.play_calls[0]) == (2, "player")

    # A passed double ends the turn: a cube decision, and no checker play.
    spy = spy_on([doubled_turn(response="pass")])
    assert [cube_context(c) for c in spy.cube_calls] == [(1, "centered")]
    assert spy.play_calls == []
    assert spy.body["turns"][0]["move"] is None
    assert spy.body["turns"][0]["cube"]["response"] == "pass"


# A position where the cube actually changes the best play: on a centered
# 1-cube and on the 2-cube a taker owns, 1-ply picks different moves.
CUBE_SENSITIVE = [0, 2, 0, 0, 1, 2, 2, -2, 2, 2, 0, 0, -3, 2, 1, 0, 1, -2, 0, -4, 0, 0, -2, -2, 0, 0]


def test_a_taken_double_is_graded_like_an_independent_post_take_request():
    engine = bgsage.create_analyzer("1ply")
    post = engine.checker_play(list(CUBE_SENSITIVE), 2, 5, cube_value=2, cube_owner="opponent")
    pre = engine.checker_play(list(CUBE_SENSITIVE), 2, 5, cube_value=1, cube_owner="centered")
    assert post.moves[0].board != pre.moves[0].board, "position must be cube-sensitive"

    turns = [{"player": 0, "board": CUBE_SENSITIVE, "doubled": True, "response": "take",
              "dice": [2, 5], "played": post.moves[0].board}]
    r = client.post("/backgammon/review", json={
        "turns": turns, "move_level": "1ply", "cube_level": "1ply", "include_luck": False})
    assert r.status_code == 200, r.text
    move = r.json()["turns"][0]["move"]
    assert move["best"]["board"] == post.moves[0].board
    assert move["best"]["equity"] == post.moves[0].equity
    assert move["grade"] == "best" and move["error"] == 0.0     # it played the best
    assert move["n_legal"] == len(post.moves)
    # The pre-offer cube would have called the same play a mistake.
    assert [m.board for m in pre.moves].index(post.moves[0].board) > 0


def test_all_results_is_off_by_default():
    turns = self_play(6, max_turns=2)
    r = client.post("/backgammon/review", json={
        "turns": turns, "move_level": "1ply", "cube_level": "1ply"})
    assert all("results" not in t["move"] for t in r.json()["turns"])
    assert ReviewRequest.model_validate({"turns": turns}).all_results is False


def test_all_results_carries_every_legal_play_in_rank_order():
    turns = self_play(6, max_turns=4)
    r = client.post("/backgammon/review", json={
        "turns": turns, "move_level": "1ply", "cube_level": "1ply",
        "top_moves": 2, "all_results": True})
    assert r.status_code == 200, r.text
    engine = bgsage.create_analyzer("1ply")
    for out, turn in zip(r.json()["turns"], turns):
        move = out["move"]
        results = move["results"]
        assert len(results) == move["n_legal"]
        assert len(move["top"]) <= 3         # top_moves is untouched by this
        expected = engine.checker_play(list(turn["board"]), *turn["dice"]).moves
        assert [e["board"] for e in results] == [m.board for m in expected]
        assert [e["equity_diff"] for e in results] == [m.equity_diff for m in expected]
        # Rank order: the best first, at no loss, and never improving after.
        assert results[0]["equity_diff"] == 0.0
        assert all(a["equity_diff"] >= b["equity_diff"]
                   for a, b in zip(results, results[1:]))
        # Compact: a board and its equity loss, nothing else.
        assert all(set(e) == {"board", "equity_diff"} for e in results)
        # The played move is in there, with the loss the grade was built from.
        played = next(e for e in results if e["board"] == move["played"]["board"])
        assert played["equity_diff"] == move["played"]["equity_diff"]


def test_all_results_leaves_the_top_and_the_grades_alone():
    turns = self_play(6, max_turns=6)
    body = {"turns": turns, "move_level": "1ply", "cube_level": "1ply"}
    off = client.post("/backgammon/review", json=body).json()
    on = client.post("/backgammon/review", json=body | {"all_results": True}).json()
    for a, b in zip(off["turns"], on["turns"]):
        assert a == {**b, "move": {k: v for k, v in b["move"].items() if k != "results"}}
    assert off["players"] == on["players"]


def test_a_roll_that_moves_nothing_lists_nothing_extra():
    # On the bar behind a closed board with 6-5. Whether the engine calls that
    # no legal move at all (a dance) or the one forced non-move, `results`
    # never claims more than n_legal, and the played board is the input board.
    danced = [0] * 26
    danced[25] = 1                                    # the mover, on the bar
    for point in range(19, 25):                       # the opponent's home, shut
        danced[point] = -2
    danced[1], danced[6], danced[8], danced[10], danced[13] = -3, 5, 3, 1, 5
    r = client.post("/backgammon/review", json={
        "turns": [{"player": 0, "board": danced, "dice": [6, 5], "played": danced}],
        "move_level": "1ply", "cube_level": "1ply", "all_results": True})
    assert r.status_code == 200, r.text
    move = r.json()["turns"][0]["move"]
    assert move["n_legal"] == len(move.get("results", []))
    assert move.get("danced") or (move["forced"] and move["played"]["board"] == danced)
    assert r.json()["players"][0]["moves"]["decisions"] == 0    # nothing to decide
