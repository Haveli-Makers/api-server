# InfluxDB monitoring: api-server

The api-server runs InfluxDB 3, watches bot health, and serves the monitoring data under `/metrics` for the
dashboard. Bots and account monitors write the data; hummingbot's `docs/INFLUXDB_MONITORING.md` covers that side, and
the dashboard repo's `INFLUXDB_MONITORING.md` covers the pages.

## Why InfluxDB

Before, a bot's data reached the api-server over MQTT, and the api-server kept only the latest values: the last
heartbeat and the latest performance. Market data was sampled into Postgres every 15 minutes. Nothing could answer
"was the bot up all night?", "when did it hang?" or "where were its orders against the price?".

A proof of concept in November 2026 tried InfluxDB 3 Core (free, open source, queried with SQL). Four days of three
bots came to 2.7 million rows in 78 MB, queries took 23–65 ms, and InfluxDB used about 0.5 GB of memory. The team
then decided:

- InfluxDB 3 Core lives in this repo's Docker compose, one per environment, only reachable from this machine. Data is
  kept 14 days.
- Bots write to it directly; MQTT stays for bot commands only.
- This API reads it; the dashboard draws the charts (no Grafana). Long-range reports stay in Postgres.

## What the api-server does

- **Runs InfluxDB 3 Core.** It's the `influxdb` service in `docker-compose.yml`: profile `monitoring`, container
  `hummingbot-influxdb`, port `127.0.0.1:8181` only. Its admin token is created inside its volume on the first start.
- **Creates the database.** At startup it creates `hbot` with 14-day retention, if it doesn't exist yet.
- **Hands bots the settings.** Every bot it deploys gets `INFLUXDB_URL` (from `INFLUXDB_BOT_URL`), `INFLUXDB_TOKEN`
  and `INFLUXDB_DATABASE`, so it writes without extra setup.
- **Watches bot health** every 5 seconds (see below).
- **Serves `/metrics`**, with the same login as the rest of the API.

All of this is off unless `INFLUXDB_ENABLED=true`.

## Setup

1. Start InfluxDB:
   ```bash
   make monitoring
   ```
   It starts InfluxDB and prints three lines: `INFLUXDB_ENABLED=true`, `INFLUXDB_TOKEN=apiv3_...` and
   `COMPOSE_PROFILES=monitoring`.
2. Add those lines to `.env` and restart the API. The log says
   `Bot monitoring on: InfluxDB http://localhost:8181, database hbot`.
3. Check it:
   ```bash
   curl -u admin:admin http://localhost:8000/metrics/health
   ```
   It should answer with `enabled: true` and `influxdb_reachable: true`.

`make stop` stops everything, InfluxDB included. The data lives in the volume `api-server_influxdb-data`. Removing it
deletes the data and the token, and the next start makes a new token.

To run the API outside Docker, export the same variables instead of using `.env`:

```bash
export INFLUXDB_ENABLED=true INFLUXDB_TOKEN=$(docker exec hummingbot-influxdb sh -c 'cat "$INFLUXDB3_ADMIN_TOKEN_FILE"' | python -c "import json,sys; print(json.load(sys.stdin)['token'])")
```

## Settings

| Variable | Default | What it does |
|---|---|---|
| `INFLUXDB_ENABLED` | `false` | Turns monitoring on |
| `INFLUXDB_URL` | `http://localhost:8181` | InfluxDB as this API reaches it |
| `INFLUXDB_BOT_URL` | `http://localhost:8181` | InfluxDB as deployed bots reach it (they use host networking) |
| `INFLUXDB_TOKEN` | – | The admin token, printed by `make monitoring` |
| `INFLUXDB_DATABASE` | `hbot` | Database name |
| `INFLUXDB_RETENTION` | `14d` | How long data is kept; applied only when the database is created |
| `INFLUXDB_WATCHER_INTERVAL` | `5` | Seconds between health checks |
| `INFLUXDB_PROCESS_TIMEOUT` | `20` | Seconds without a process heartbeat before a bot is down |
| `INFLUXDB_STRATEGY_TIMEOUT` | `10` | Seconds without a strategy heartbeat before its loop is down |

## Endpoints

Every endpoint except `/metrics/health` answers 503 while monitoring is off. Windows are given in minutes, from 5 up
to 4320 (3 days), with an optional `end` time (UTC).

| Group | Endpoints |
|---|---|
| Status | `GET /metrics/health`: monitoring on, InfluxDB reachable, the watcher's last check |
| Accounts | `/metrics/accounts`, `/metrics/balances`, `/metrics/account-orders` (open orders now, whoever placed them), `/metrics/fills`. Each takes `account=` and `exchange=`, except `/accounts`, which lists them. |
| Bots | `/metrics/uptime`, `/metrics/uptime/timeline`, `/metrics/incidents`, `/metrics/status-changes`, `/metrics/bots`, `/metrics/pnl`, `/metrics/orders?bot=`, `/metrics/ladder?bot=` |
| Markets | `/metrics/markets`, `/metrics/feeds`, `/metrics/market?exchange=&pair=` (bid, ask, mid, spread, depth, book age) |

The README section "Bot Monitoring (InfluxDB)" lists every table and its fields.

## Bot health watcher

`services/bot_health_watcher.py` checks every bot's heartbeats every 5 seconds:

- **hung:** the strategy loop has been silent for 10 s while the process still beats.
- **down:** the process has been silent for 20 s.
- **stopped:** the bot wrote a clean stop. It isn't an incident, and the time doesn't count against uptime.

It writes `bot_up` (up or down per check) and `bot_status_change` (each change), and logs each change as
`Bot status: <bot>: ...`. The first check after the API starts is silent. Account monitors are bots too, so they show
up here.

## Limits and security

- **3 days per query.** InfluxDB 3 Core reads about 72 hours of a table in one query; longer windows get HTTP 422.
- **Retention is set once.** It's applied when the database is created. Changing it later needs a manual change in
  InfluxDB.
- **Admin token only.** InfluxDB 3 Core has no scoped tokens, so InfluxDB listens on `127.0.0.1` only. Never publish
  port 8181, and keep the token out of shared files.
- **Names are checked.** Names that go into SQL (bot, account, exchange, pair) may only contain letters, digits and
  `. _ -`; anything else gets HTTP 400.
- **No automated tests in this repo.** It was checked end to end against a real InfluxDB and the real dashboard pages,
  with scripts kept outside the repo.
- **No alerts yet.** Sending alerts (for example to Google Chat) is on hold.

## Files

- `services/influxdb_client.py`: queries and writes; creates the database.
- `services/bot_health_watcher.py`: hung, down and stopped detection.
- `routers/metrics.py`: the `/metrics` endpoints.
- `utils/line_protocol.py`, `utils/order_segments.py`: writing rows, and turning order events into lines on the chart.
- `config.py` (`InfluxDBSettings`), `main.py` (startup), `services/docker_service.py` (bot settings),
  `docker-compose.yml` and `Makefile` (`monitoring`, `stop`).

## Troubleshooting

| You see | Why, and what to do |
|---|---|
| 503 "Bot monitoring is off" | `INFLUXDB_ENABLED` isn't `true`; set it and restart |
| `/metrics/health`: `influxdb_reachable: false` | InfluxDB isn't running (`make monitoring`), or `INFLUXDB_URL` is wrong |
| 401 from InfluxDB in the log | Wrong token. Wiping the volume makes a new one: run `make monitoring` again and update `.env`. |
| 422 "Window too large" | Use a window of 3 days or less |
| A bot never shows up | It wasn't started with the `INFLUXDB_*` settings (bots deployed before monitoring was on), or it hasn't run a strategy yet |
| No `Bot status` lines in the log | Normal for the first check. If none ever appear, the watcher isn't running: see `/metrics/health`. |
