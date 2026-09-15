import random

import bgsage
from fastapi.testclient import TestClient

from app.main import app

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
    assert body["levels"] == {"moves": "1ply", "cube": "2ply"}
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
