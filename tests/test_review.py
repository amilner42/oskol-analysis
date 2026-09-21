import random

import bgsage
from fastapi.testclient import TestClient

from app import pool
from app.main import analyzer, app
from app.review import ReviewRequest, analyze_serially, review_game

client = TestClient(app)
START = bgsage.STARTING_BOARD


def dance_board() -> list[int]:
    """The mover is on the bar and every entry point is closed."""
    board = [0] * 26
    board[25] = 1
    board[6] = 14
    board[1] = -3
    for point in range(19, 25):
        board[point] = -2
    return board


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


def test_a_dance_is_not_counted_as_a_forced_move():
    board = dance_board()
    turns = [{"player": 0, "board": board, "dice": [1, 2], "played": board}]

    r = client.post("/backgammon/review", json={
        "turns": turns,
        "move_level": "1ply",
        "cube_level": "1ply",
        "include_luck": False,
    })

    assert r.status_code == 200, r.text
    assert r.json()["turns"][0]["move"] == {"danced": True, "n_legal": 0}
    assert r.json()["players"][0]["moves"] == {
        "decisions": 0, "forced": 0, "error": 0.0, "grades": {}}


def test_an_empty_legal_move_list_is_also_a_dance(monkeypatch):
    board = dance_board()
    monkeypatch.setattr(bgsage, "possible_moves", lambda *_args: [])
    turns = [{
        "player": 0,
        "board": board,
        "away1": 1,
        "away2": 3,
        "is_crawford": True,
        "dice": [1, 2],
        "played": board,
    }]

    r = client.post("/backgammon/review", json={"turns": turns, "include_luck": False})

    assert r.status_code == 200, r.text
    assert r.json()["turns"][0]["move"] == {"danced": True, "n_legal": 0}


def test_a_dance_requires_the_unchanged_played_board():
    board = dance_board()
    turns = [{"player": 0, "board": board, "dice": [1, 2], "played": None}]

    r = client.post("/backgammon/review", json={"turns": turns})

    assert r.status_code == 422
    assert "played must equal the unchanged board" in r.json()["detail"]


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
