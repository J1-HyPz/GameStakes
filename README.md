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

**Status: Phase 2 (data model).** The stack runs and the full schema is in
place — 25 tables covering fixtures, odds time-series, simulations, bets and
settlement, with 35 leagues across 5 sports seeded from YAML. Data providers,
models and the bet builder land in later phases. See
[Build phases](#build-phases).

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
| 3 | Provider adapters, rate limiting, failover, settings page | — |
| 4 | Schedule UI with live scores | — |
| 5 | Football model (Dixon-Coles + Elo) and simulation engine | — |
| 6 | Odds ingestion, de-vigging, edge calculation | — |
| 7 | Bet builder with correlated parlay pricing | — |
| 8 | Settlement, tracker, calibration | — |
| 9 | Basketball, American football, MMA, boxing | — |
| 10 | Backtesting harness | — |
| 11 | Polish, TrueNAS manifest, tagged release | — |

## TrueNAS SCALE

A ready-to-use custom app manifest ships in Phase 11
([`docker/truenas-app.yaml`](docker/truenas-app.yaml) is currently a stub).
The image is multi-arch (amd64 + arm64) and published to
`ghcr.io/j1-hypz/gamestakes`.

## License

[MIT](LICENSE)
