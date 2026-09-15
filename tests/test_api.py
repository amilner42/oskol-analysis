import bgsage
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
START = bgsage.STARTING_BOARD


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


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
