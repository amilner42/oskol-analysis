import bgsage
import pytest
from fastapi.testclient import TestClient

from app import main, pool
from app.main import app

client = TestClient(app)
START = bgsage.STARTING_BOARD


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_health_says_which_threads_a_request_gets():
    # An operator reading /health has to be able to tell a machine that gives
    # a lone request everything from one that does not.
    body = client.get("/health").json()
    assert body["review_workers"] == pool.workers()
    assert body["engine_threads"] == pool.engine_threads()
    assert body["solo_engine_threads"] == pool.solo_threads()


def test_solo_threads_is_the_whole_machine_and_still_a_knob(monkeypatch):
    assert pool.solo_threads() == pool.CORES
    monkeypatch.setenv("SOLO_ENGINE_THREADS", "3")
    assert pool.solo_threads() == 3


def test_a_wide_engine_is_a_second_engine_and_not_a_changed_one():
    # An engine's thread count is fixed when it is created, so the wide one
    # has to be its own cache entry: anything already holding the default
    # engine keeps it, exactly as it was.
    default = main.analyzer("1ply")
    wide = main.solo_analyzer("1ply")
    assert wide is not default
    assert main.analyzer("1ply") is default
    assert main.solo_analyzer("1ply") is wide


def test_opening_31_plays_the_5_point():
    r = client.post("/backgammon/moves", json={"board": START, "dice": [3, 1], "level": "1ply"})
    assert r.status_code == 200
    body = r.json()
    best = body["moves"][0]
    # 8/5 6/5: two checkers land on the 5-point
    assert best["board"][5] == 2
    assert best["equity_diff"] == 0.0
    assert len(body["moves"]) == 16
    assert 0.4 < best["probs"]["win"] < 0.7


def test_opening_is_no_double_take():
    r = client.post("/backgammon/cube", json={"board": START, "level": "1ply"})
    assert r.status_code == 200
    body = r.json()
    assert body["should_double"] is False
    assert body["should_take"] is True
    assert body["equity_dp"] == 1.0


def test_post_move_position():
    r = client.post("/backgammon/position", json={"board": START, "level": "1ply"})
    assert r.status_code == 200
    probs = r.json()["probs"]
    assert 0 <= probs["gammon_win"] <= probs["win"] <= 1


def test_batch_keeps_order():
    r = client.post("/backgammon/batch", json={"items": [
        {"kind": "cube", "request": {"board": START, "level": "1ply"}},
        {"kind": "moves", "request": {"board": START, "dice": [3, 1], "level": "1ply"}},
    ]})
    assert r.status_code == 200
    results = r.json()["results"]
    assert "should_double" in results[0]
    assert "moves" in results[1]


def test_rejects_bad_board():
    bad = list(START)
    bad[24] = 20
    r = client.post("/backgammon/cube", json={"board": bad})
    assert r.status_code == 422


def test_rejects_bad_batch_item_before_running():
    r = client.post("/backgammon/batch", json={"items": [
        {"kind": "moves", "request": {"board": START, "dice": [7, 1]}},
    ]})
    assert r.status_code == 422


def test_match_play_cube():
    r = client.post("/backgammon/cube", json={"board": START, "away1": 2, "away2": 2, "level": "1ply"})
    assert r.status_code == 200
    assert r.json()["optimal_action"] in ("No Double", "Double/Take", "Double/Pass")


# ---------- /backgammon/rolls ----------


def test_rolls_gives_a_row_per_roll():
    r = client.post("/backgammon/rolls", json={"board": START, "level": "2ply"})
    assert r.status_code == 200
    body = r.json()
    rows = body["rows"]
    assert len(rows) == 21
    assert sum(row["weight"] for row in rows) == 36
    assert body["level"] == "2ply"
    mean = sum(row["weight"] * row["equity"] for row in rows) / 36
    assert mean == pytest.approx(body["equity"], abs=1e-6)
    # Every row says what it would play, which is what the map writes in a cell.
    assert all(row["best"] for row in rows)


def test_rolls_refuses_the_depths_that_cannot_work():
    # 4-ply details corrupt the analysis; 1-ply has no per-roll layer at all.
    # Both are refused by the type, before a board reaches the engine.
    for level in ("4ply", "1ply", "rollout"):
        r = client.post("/backgammon/rolls", json={"board": START, "level": level})
        assert r.status_code == 422, level


def test_rolls_takes_a_board_with_a_checker_on_the_bar():
    # /moves and /cube refuse this board; a grid must not, because it happens
    # several times a game (bgsage-gotchas).
    board = list(START)
    board[0] = 1          # the opponent's bar, as bgsage counts it
    board[19] = -4
    r = client.post("/backgammon/rolls", json={"board": board, "level": "2ply"})
    assert r.status_code == 200, r.text
    assert len(r.json()["rows"]) == 21
    assert client.post("/backgammon/cube", json={"board": board, "level": "1ply"}).status_code == 422


def test_rolls_in_a_batch_matches_the_lone_route():
    ask = {"board": START, "level": "2ply"}
    one = client.post("/backgammon/rolls", json=ask).json()
    batched = client.post("/backgammon/batch", json={
        "items": [{"kind": "rolls", "request": ask}, {"kind": "rolls", "request": ask}],
    })
    assert batched.status_code == 200
    results = batched.json()["results"]
    assert results == [one, one]
