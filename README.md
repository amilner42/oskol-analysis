# oskol-analysis

The [Open Sage](https://github.com/markbgsage/bgsage) backgammon engine
(MPL-2.0, XG-strength) behind a small private HTTP API, for
[Oskol](https://oskol.io)'s backgammon analysis.

Runs on Fly as `oskol-analysis` (performance-4x: 4 dedicated cores, 8 GB)
with no public IP. Inside the Fly org it
answers at `http://oskol-analysis.flycast`; the machine stops when idle and
starts on the next request (a few seconds of cold start).

## API

Routes are namespaced by game (`/backgammon/...`); there is no auth, the
network is the boundary.

Every position is Open Sage's 26-int board, **from the perspective of the
player on roll**:

| index   | meaning |
|---------|---------|
| 1..24   | points counted from the on-roll player's side; theirs positive, the opponent's negative. They move 24 → 1 and bear off past 1. |
| 25      | the on-roll player's bar (nonnegative count) |
| 0       | the opponent's bar (nonnegative count) |

The bar slots are counts, so unlike occupied points they do not use opposing
signs. Borne-off checkers are not stored: for the on-roll player subtract
positive interior checkers plus `board[25]` from 15; for the opponent subtract
the absolute negative interior checkers plus `board[0]`. The opening position is
`[0,-2,0,0,0,0,5,0,3,0,0,0,-5,5,0,0,0,-3,0,-5,0,0,0,0,2,0]`.

Common fields on every request, all optional except `board`:

```json
{"board": [...], "cube_value": 1, "cube_owner": "centered",
 "away1": 0, "away2": 0, "is_crawford": false, "jacoby": true, "level": "2ply"}
```

`cube_owner` is `centered`, `player` (the one on roll) or `opponent`.
`away1`/`away2` are points still needed by the on-roll player and the
opponent; both `0` means a money game. `level` is one of `1ply` `2ply`
`3ply` `4ply` `truncated1` `truncated2` `truncated3` `rollout`. Each extra
ply costs roughly 20x. `/moves` defaults to `2ply` and `/cube` to `3ply`, the
quick setting; `/review` defaults to `4ply` for both.

| route | extra fields | returns |
|-------|--------------|---------|
| `POST /backgammon/moves` | `dice: [d1, d2]`, `include_game_plans` | every legal play best first: `board`, `equity`, `cubeless_equity`, `equity_diff`, `probs` |
| `POST /backgammon/cube` | | `equity_nd`, `equity_dt`, `equity_dp`, `should_double`, `should_take`, `optimal_action`, `probs` |
| `POST /backgammon/position` | | a post-move position, for the player who just moved: `cubeful_equity`, `cubeless_equity`, `probs` |
| `POST /backgammon/review` | `turns`, `jacoby`, `move_level`, `cube_level`, `top_moves`, `include_luck` | a whole game graded, see below |
| `POST /backgammon/batch` | `items: [{kind, request}]` | `results` in the same order; a bad item 422s the whole batch first |
| `GET /health` | | `ok`, `model`, `levels`, `review_workers`, `engine_threads` |

Probabilities are `win`, `gammon_win`, `backgammon_win`, `gammon_loss`,
`backgammon_loss`, from the on-roll player's view (for `/position`, the
player who just moved). Equities are cubeful unless named cubeless.

## Reviewing a whole game

`POST /backgammon/review` takes the game as one entry per turn, each from
the perspective of the player on roll, and grades every decision, by
default at 4-ply (what XG's own analysis reads as accurate):

```json
{"jacoby": true, "move_level": "4ply", "cube_level": "4ply", "top_moves": 5,
 "include_luck": true,
 "turns": [
   {"player": 0, "board": [...], "cube_value": 1, "cube_owner": "centered",
    "away1": 0, "away2": 0, "is_crawford": false,
    "doubled": false, "response": null,
    "dice": [3, 1], "played": [...]}
 ]}
```

`player` is 0 or 1 (who is on roll), and `played` is the board after the move,
still from the mover's view. When the roll cannot be played, `played` must be
the unchanged input board.
A turn that doubles carries `doubled: true` and the opponent's `response`
(`take` or `pass`); a passed double has no dice and no move. Cube state and
match score are per turn, so the caller does not need to track them here.

Each turn comes back with:

- `cube`: when a double was legal, the analysis (`equity_nd`, `equity_dt`,
  `equity_dp`, `optimal_action`, probabilities), the `action` taken, and a
  verdict for the `doubler` and, after a double, the `taker`: `error` in
  equity, `grade` (`ok`, `doubtful`, `bad`, `very_bad`, XG's bands at
  0.02/0.08/0.16) and `mistake` (`missed_double`, `wrong_double`,
  `wrong_take`, `wrong_pass` or null). `null` when no double was possible.
- `move`: the `played` move and the `best` move (each with `notation` such
  as `8/5 6/5` or `bar/22*`, `rank`, `equity`, `equity_diff`, `probs`,
  `board`), `top` (the top N, plus the played move if it ranked lower),
  `n_legal`, `forced`, `error` and `grade` (`best` or the bands above).
  A dance is `{"danced": true, "n_legal": 0}` and is not counted as a forced
  move.
- `luck`: how lucky the roll was in equity, from the roller's view (needs a
  cube level of 2-ply or more). It reads the per-roll equities of a cube
  analysis at the cube level, but never deeper than 3-ply (so 2-ply luck,
  `level_label`): bgsage's 4-ply cube analysis goes wrong when asked for
  those per-roll details, so at 4-ply the graded cube analysis runs without
  them and luck gets its own 3-ply one.

`players[0]` and `players[1]` total it up: move decisions (forced ones
excluded), errors, grade counts, cube decisions and mistakes, luck, and
`pr`, XG's Performance Rating: equity lost per unforced decision times 500.
The response also echoes `levels` (`{move, cube, luck}`, the analysis
levels used; `luck` is null when no luck was computed) and `timing_ms`, the
review's wall time.

Bad input 422s with the turn index before any engine time is spent (a
double that was not legal, a played board that is not a legal move for the
dice). Then the turns fan out over a process pool (`app/pool.py`): one
worker per core, each loading its engines once and keeping them, results
back in turn order. A turn's analysis depends only on that turn, so the
parallel review returns exactly what a serial one would. `REVIEW_WORKERS`
(default: the cores) and `REVIEW_ENGINE_THREADS` (bgsage threads per worker,
default cores / workers, at least 2) tune it; `REVIEW_WORKERS=1` reviews
serially in the server process. A 4-ply review is mostly the checker plays
(about 6 CPU-seconds a turn on an M-series core, more on a busy midgame):
a 70-turn game took 111 s locally with 4 workers.

## Run locally

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/uvicorn app.main:app --port 8080
curl -s localhost:8080/backgammon/moves -H 'content-type: application/json' \
  -d '{"board":[0,-2,0,0,0,0,5,0,3,0,0,0,-5,5,0,0,0,-3,0,-5,0,0,0,0,2,0],"dice":[3,1]}'
```

## Deploy

CI deploys `main` with the `FLY_API_TOKEN` secret. By hand: `fly deploy`.
To call the deployed app from your laptop:

```sh
fly proxy 8080:8080 -a oskol-analysis   # then curl localhost:8080/health
```

The app must keep its Flycast-only networking: `fly ips list` should show
one private address and nothing public.
