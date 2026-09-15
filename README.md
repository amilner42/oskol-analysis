# oskol-analysis

The [Open Sage](https://github.com/markbgsage/bgsage) backgammon engine
(MPL-2.0, XG-strength) behind a small private HTTP API, for
[Oskol](https://oskol.io)'s backgammon analysis.

Runs on Fly as `oskol-analysis` with no public IP. Inside the Fly org it
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
| 25      | the on-roll player's bar (positive) |
| 0       | the opponent's bar (negative) |

Borne-off checkers are not stored. The opening position is
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
ply costs roughly 20x; `2ply` moves and `3ply` cubes is a good review setting.

| route | extra fields | returns |
|-------|--------------|---------|
| `POST /backgammon/moves` | `dice: [d1, d2]`, `include_game_plans` | every legal play best first: `board`, `equity`, `cubeless_equity`, `equity_diff`, `probs` |
| `POST /backgammon/cube` | | `equity_nd`, `equity_dt`, `equity_dp`, `should_double`, `should_take`, `optimal_action`, `probs` |
| `POST /backgammon/position` | | a post-move position, for the player who just moved: `cubeful_equity`, `cubeless_equity`, `probs` |
| `POST /backgammon/batch` | `items: [{kind, request}]` | `results` in the same order; a bad item 422s the whole batch first |
| `GET /health` | | `ok`, `model`, `levels` |

Probabilities are `win`, `gammon_win`, `backgammon_win`, `gammon_loss`,
`backgammon_loss`, from the on-roll player's view (for `/position`, the
player who just moved). Equities are cubeful unless named cubeless.

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
