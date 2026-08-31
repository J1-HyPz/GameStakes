# GameStakes

[![CI](https://github.com/J1-HyPz/GameStakes/actions/workflows/ci.yml/badge.svg)](https://github.com/J1-HyPz/GameStakes/actions/workflows/ci.yml)
[![Docker](https://github.com/J1-HyPz/GameStakes/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/J1-HyPz/GameStakes/actions/workflows/docker-publish.yml)

Self-hosted multi-sport prediction and bet building platform. Ingests schedules,
results, statistics and bookmaker odds; produces **probabilistic** predictions via
Monte Carlo simulation; builds risk-tiered bets with honest edge and stake
recommendations; and tracks every prediction against real outcomes.

> **Honesty first.** Every prediction is a distribution with a confidence score,
> never a bare number. The bet builder returns *nothing* rather than a marginal
> bet. Models can be wrong; losing runs are expected even with a real edge.
> Nothing in this tool is financial advice.

**Status: all 11 phases complete.** Providers ingest fixtures, results and
odds; models cover football, basketball, American football, MMA and boxing;
the bet builder prices correlated parlays and sizes stakes; the tracker grades
outcomes and reports calibration; and the backtester replays history with
walk-forward refitting. See [Build phases](#build-phases).

### Data model notes

- **Combat sports are first-class.** A fixture is a match *or* a bout; its
  participants are two teams *or* two fighters (enforced by a CHECK
  constraint), with method/round detail in `bout_results`.
- **Odds are append-only.** Every capture is a new `odds_snapshots` row —
  closing line value needs the price history, not the latest price.
- **Predictions are reproducible.** Each records its model version, RNG seed,
  input feature hash and generation time.
- **Entity resolution never guesses.** Provider names resolve by external id,
  learned alias, exact match, then fuzzy match — and only when the score clears
  a threshold, beats the runner-up by a margin, and neither name carries extra
  tokens (so a women's or B team can't be mapped onto the first team).
  Everything else goes to a review queue at `/api/resolution/queue`.
- **Leagues are configuration.** Add one to
  [`leagues.yaml`](backend/app/ingest/seeds/leagues.yaml) — no code change.
  Seeding is idempotent; removed leagues deactivate rather than delete.

### How the predictions work

- **Every prediction is a distribution.** A fixture is simulated 20,000 times
  (configurable to 100,000) and markets are derived from the draws, so the UI
  shows intervals rather than a single number.
- **Parlays are priced from the joint distribution.** Legs in the same game
  correlate: if a team wins big, the over and its striker scoring both become
  likelier. The builder counts the iterations where *every* leg wins, and shows
  the naive independent figure beside it so the difference is visible.
- **De-vigging uses the power method**, not proportional normalisation, which
  overstates longshots and invents edges that are not there.
- **Fractional Kelly, capped.** Quarter Kelly by default, hard-capped per tier
  and by daily/weekly exposure limits the builder will not exceed.
- **A tier with nothing qualifying returns nothing** and says which filter
  emptied the pool. Filling the slot with a marginal bet is how a tool teaches
  its user to lose money.
- **Sample size is impossible to ignore.** Every rate ships with a bootstrap
  confidence interval and its n; the calibration chart shows whether stated
  probabilities can be trusted at all.

### Known data limits

These are constraints of the free tiers, stated plainly rather than papered
over:

- **The Odds API free tier is 500 credits a month**, billed per market per
  region. Snapshots are therefore sparse, and "closing line" means the last
  price captured before kickoff, not a true close.
- **Boxing coverage is thin.** No free structured API exists and BoxRec
  prohibits scraping, so coverage is event listings and metadata only.
- **Injury data is manual.** No reliable free feed exists, so key-player
  availability is an explicit model input rather than something inferred.

## Quick start

### Docker (recommended)

Optional first step — configuration (the app also runs with zero config):

```bash
cp .env.example .env
```

Full stack (PostgreSQL + Redis; Compose v2.24+). Set `POSTGRES_PASSWORD` in
your shell (or `docker/.env`) **before the first start** — the database volume
captures it at initialisation:

```bash
cd docker && POSTGRES_PASSWORD=change-me docker compose up -d
```

Slim single container (SQLite, no Redis) for low-resource hosts:

```bash
cd docker && docker compose -f docker-compose.slim.yml up -d
```

Open http://localhost:8080 — the dashboard shows live system status, and
`/api/health` reports every component.

### Local development

```bash
# Backend (Python 3.12)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"
cd backend && alembic upgrade head
uvicorn app.main:app --reload --port 8080

# Frontend (Node 18+; separate terminal — dev server proxies /api to :8080)
cd frontend && npm install && npm run dev
```

To serve the SPA from FastAPI like production does: `cd frontend && npm run build`,
then restart uvicorn.

## Configuration

All configuration is via environment variables with working defaults — the app
starts with **zero configuration** (SQLite, no Redis, no provider keys) and
shows which features are unavailable. See [`.env.example`](.env.example) for
every variable, documented, with links for obtaining provider keys.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8080` | Single port for UI + API |
| `DATABASE_URL` | SQLite in `./data` | PostgreSQL (full) or SQLite (slim) |
| `REDIS_URL` | *(empty)* | Optional cache/broker; empty = in-process |
| `ROOT_PATH` | *(empty)* | Serve under a subpath behind a reverse proxy |
| `AUTH_ENABLED` | `false` | Optional single-user login for non-LAN use |
| `TIMEZONE` / `CURRENCY` | `Europe/London` / `GBP` | Display settings |
| `PUID` / `PGID` | `1000` | Container runtime user (match dataset owner) |
| `POSTGRES_PASSWORD` | `gamestakes` | Full-stack DB password — compose interpolation (shell or `docker/.env`), set before first start |

In Docker, the compose files forward the repo-root `.env` into the container,
except stack wiring (`DATABASE_URL`, `REDIS_URL`, container `PORT`), which they
pin. To change the **host** port, export `PORT` when running compose.

## Development

```bash
# Backend checks
ruff check backend && ruff format --check backend
cd backend && mypy app && pytest

# Frontend checks
cd frontend && npm run lint && npm test && npm run build
```

API types are generated from the OpenAPI schema (no hand-written duplicates):

```bash
cd backend && python -m app.scripts.export_openapi > openapi.json
cd ../frontend && npm run openapi
```

CI fails if `frontend/src/types/api.ts` is stale.

## Build phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Skeleton: Docker, CI, health endpoint, SPA serving, migrations | ✅ |
| 2 | Full data model + entity resolution | ✅ |
| 3 | Provider adapters, rate limiting, failover, settings page | ✅ |
| 4 | Schedule UI with live scores | ✅ |
| 5 | Football model (Dixon-Coles + Elo) and simulation engine | ✅ |
| 6 | Odds ingestion, de-vigging, edge calculation | ✅ |
| 7 | Bet builder with correlated parlay pricing | ✅ |
| 8 | Settlement, tracker, calibration | ✅ |
| 9 | Basketball, American football, MMA, boxing | ✅ |
| 10 | Backtesting harness | ✅ |
| 11 | Polish, TrueNAS manifest, tagged release | ✅ |

## TrueNAS SCALE

The image is multi-arch (amd64 + arm64) and published publicly to
`ghcr.io/j1-hypz/gamestakes`, so no registry credentials are needed.

### 1. Create the datasets

**Storage → Datasets**, select your pool, and add three datasets under a
`gamestakes` parent (Add Dataset, one level at a time):

```
<pool>/apps/gamestakes/config
<pool>/apps/gamestakes/data
<pool>/apps/gamestakes/logs
```

`data` holds the SQLite database and the stored simulation draws, so it is the
one worth including in a snapshot task.

### 2. Find the UID and GID that own them

This is the step that most often goes wrong. From **System → Shell**:

```bash
ls -na /mnt/<pool>/apps/gamestakes
```

The third and fourth columns are the numeric UID and GID. Use those exact
numbers for `PUID`/`PGID` in the manifest. If they do not match, the container
cannot write to `/data`, migrations fail, and the app restarts in a loop.

If the datasets are owned by root (`0:0`) and you would rather not run as root,
change the owner first: **Datasets → (select) → Permissions → Edit**, or

```bash
chown -R 1000:1000 /mnt/<pool>/apps/gamestakes
```

### 3. Install the app

**Apps → Discover Apps → Custom App**, then choose **Install via YAML** if your
build offers it (TrueNAS SCALE 24.10 "Electric Eel" and later, which run Docker
natively). Paste [`docker/truenas-app.yaml`](docker/truenas-app.yaml), changing:

- `<pool>` in the three volume paths → your pool name
- `PUID` / `PGID` → the numbers from step 2

On **older, k3s-based releases** (23.10 and earlier) there is no YAML installer.
Use **Launch Docker Image** and transcribe the same values into the form:
image `ghcr.io/j1-hypz/gamestakes:0.1.0`, port forward host `8100` →
container `8080`, three host-path volumes, and the environment variables from
the manifest.

Then open **http://&lt;truenas-ip&gt;:8100**.

The manifest publishes on host port **8100**. To move it, change only the
left-hand number in `ports` (`"8100:8080"`) — the container always listens on
8080 internally.

### 4. First run

The app starts with no data and will say so rather than showing empty tables.
To get fixtures flowing:

1. **Settings → Data sources** shows every provider and what it unlocks. Add at
   least `FOOTBALL_DATA_ORG_KEY` (free and instant from
   [football-data.org](https://www.football-data.org/client/register)) to the
   app's environment variables and restart it. `THE_ODDS_API_KEY` is what the
   bet builder needs for prices.
2. **Settings → Ingestion jobs → Run "Refresh upcoming fixtures"**, then
   "Refresh recent results". The scheduler then keeps them current on its own.
3. **Set `BANKROLL`** when you want staking. The builder refuses to size a bet
   without one, because every stake is expressed as a share of bankroll.
4. Predictions need roughly 20 finished matches in a league before the model
   will fit. Until then the fixture pages say so instead of guessing.

### 5. Updating

Pushes to `main` publish `:latest` and a `:sha-<commit>` tag; a version tag
(`v0.2.0`) also publishes `:0.2.0` and `:0.2`.

The manifest pins `:0.1.0` deliberately — a pinned tag means a redeploy gives
you the same build rather than silently changing under you. To take a new
version, **Apps → (app) → Edit**, change the image tag, and save.

If you prefer automatic updates, change the tag to `:latest` and use the app's
**Update** / pull action. Note that TrueNAS caches image layers, so pulling
`:latest` may need an explicit pull to actually fetch the new digest.

Database migrations run automatically on every start, so upgrading is just
changing the tag — no manual migration step.

### Behind a reverse proxy

Set `ROOT_PATH` (e.g. `/gamestakes`) and forward the `Host` and `X-Forwarded-*`
headers. If the app is reachable from outside your LAN, also set
`AUTH_ENABLED=true` with an `AUTH_PASSWORD` and a `JWT_SECRET`
(`openssl rand -hex 32`).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Container restarts in a loop | Almost always `PUID`/`PGID` not matching the dataset owner. Check the app logs: the entrypoint warns if it cannot chown, then migrations fail with a permission error naming the path. |
| Deploy fails pulling the image | The tag does not exist. Check the [published versions](https://github.com/J1-HyPz/GameStakes/pkgs/container/gamestakes); the image itself is public and needs no login. |
| Port already allocated | Something else holds 8100 on the host. Change the left-hand number in `ports`. |
| UI loads but no fixtures | No provider keys yet, or no ingestion job has run. Settings → Data sources, then Ingestion jobs. |
| Fixtures exist but no predictions | The league needs ~20 finished matches to fit the model. Run "Refresh recent results" to backfill history. |
| "No qualifying bet" on every tier | Working as intended when nothing clears the thresholds. Each tier names the filter that emptied the pool — that is the honest answer, not a fault. |
| Odds stop updating | The Odds API free tier is 500 credits a month. Settings → Data sources shows credits remaining; health reports `degraded` below 50. |
| Assets 404 behind a proxy | `ROOT_PATH` unset, or the proxy strips the subpath before forwarding. |

## License

[MIT](LICENSE)
