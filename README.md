# Hummingbot API

A REST API for managing Hummingbot trading bots across multiple exchanges, with AI assistant integration via MCP.

## Quick Start

```bash
git clone https://github.com/hummingbot/hummingbot-api.git
cd hummingbot-api
make setup    # Creates .env (prompts for passwords)
make deploy   # Starts all services
```

That's it! The API is now running at http://localhost:8000

## Available Commands

| Command | Description |
|---------|-------------|
| `make setup` | Create `.env` file with configuration |
| `make deploy` | Start all services (API, PostgreSQL, EMQX) |
| `make stop` | Stop all services |
| `make run` | Run API locally in dev mode |
| `make install` | Install conda environment for development |
| `make build` | Build Docker image |
| `make monitoring` | Start InfluxDB for bot monitoring and print its `.env` lines |

## Services

After `make deploy`, these services are available:

| Service | URL | Description |
|---------|-----|-------------|
| **API** | http://localhost:8000 | REST API |
| **Swagger UI** | http://localhost:8000/docs | Interactive API documentation |
| **PostgreSQL** | localhost:5432 | Database |
| **EMQX** | localhost:1883 | MQTT broker |
| **EMQX Dashboard** | http://localhost:18083 | Broker admin (admin/public) |
| **InfluxDB** | localhost:8181 (this machine only) | Bot monitoring, optional: see [Bot Monitoring](#bot-monitoring-influxdb) |

## Connect AI Assistant (MCP)

### Claude Code (CLI)

```bash
claude mcp add --transport stdio hummingbot -- \
  docker run --rm -i \
  -e HUMMINGBOT_API_URL=http://host.docker.internal:8000 \
  -v hummingbot_mcp:/root/.hummingbot_mcp \
  hummingbot/hummingbot-mcp:latest
```

Then use natural language:
- "Show my portfolio balances"
- "Set up my Binance account"
- "Create a market making strategy for ETH-USDT"

### Claude Desktop

Add to your config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "hummingbot": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "HUMMINGBOT_API_URL=http://host.docker.internal:8000", "-v", "hummingbot_mcp:/root/.hummingbot_mcp", "hummingbot/hummingbot-mcp:latest"]
    }
  }
}
```

Restart Claude Desktop after adding.

## Gateway (DEX Trading)

Gateway enables decentralized exchange trading. Start it via MCP:

> "Start Gateway in development mode with passphrase 'admin'"

Or via API at http://localhost:8000/docs using the Gateway endpoints.

Once running, Gateway is available at http://localhost:15888

## Configuration

The `.env` file contains all configuration. Key settings:

```bash
USERNAME=admin              # API username
PASSWORD=admin              # API password
CONFIG_PASSWORD=admin       # Encrypts bot credentials
DATABASE_URL=...            # PostgreSQL connection
GATEWAY_URL=...             # Gateway URL (for DEX)
```

Edit `.env` and restart with `make deploy` to apply changes.

## API Features

- **Portfolio**: Balances, positions, P&L across all exchanges
- **Trading**: Place orders, manage positions, track history
- **Bots**: Deploy, monitor, and control trading bots
- **Market Data**: Prices, orderbooks, candles, funding rates
- **Strategies**: Create and manage trading strategies

Full API documentation at http://localhost:8000/docs

## Bot Monitoring (InfluxDB)

Optional, and off by default. Bots write their heartbeats, prices, orders and PnL to InfluxDB 3 Core. The API
watches the heartbeats and serves the `/metrics` endpoints: uptime, incidents (hang or crash), PnL, orders vs price,
and market feeds.

**Turn it on**

1. Run `make monitoring`. It starts InfluxDB, reachable only from this machine on `localhost:8181`, and prints the
   lines to add to `.env`:
   ```bash
   INFLUXDB_ENABLED=true
   INFLUXDB_TOKEN=apiv3_...
   COMPOSE_PROFILES=monitoring    # so make deploy starts InfluxDB too
   ```
2. Restart the API. Bots it deploys from then on get `INFLUXDB_URL`, `INFLUXDB_TOKEN` and `INFLUXDB_DATABASE`.

InfluxDB creates its admin token on its first start and keeps it in the `influxdb-data` volume. The API creates the
database on startup, with data kept for 14 days.

**How the health watcher decides**

- Every 5 s it reads each bot's two heartbeats. The process heartbeat comes from a background thread, so it stops
  only when the process dies. The strategy heartbeat comes from the strategy loop.
- A bot whose strategy heartbeat is silent for 10 s while its process still beats is **hung**. A bot whose process
  heartbeat is silent for 20 s is **down** (a crash).
- A bot that stops on purpose writes a `stopped` event first. It counts as stopped, not down, and its stopped time is
  left out of its uptime.
- Each change is stored in `bot_status_change` and logged. No notifications are sent yet.

**Settings** (`.env`)

| Setting | Default | What it is |
|---------|---------|------------|
| `INFLUXDB_ENABLED` | `false` | Turn bot monitoring on |
| `INFLUXDB_URL` | `http://localhost:8181` | InfluxDB as the API reaches it (Docker Compose sets `http://influxdb:8181`) |
| `INFLUXDB_BOT_URL` | `http://localhost:8181` | InfluxDB as the bots reach it (they use host networking) |
| `INFLUXDB_TOKEN` | | Admin token, printed by `make monitoring` |
| `INFLUXDB_DATABASE` | `hbot` | Database the bots write to |
| `INFLUXDB_RETENTION` | `14d` | How long data is kept; applies when the database is created |
| `INFLUXDB_WATCHER_INTERVAL` | `5` | Seconds between health checks |
| `INFLUXDB_PROCESS_TIMEOUT` | `20` | Seconds without a process heartbeat before a bot is down |
| `INFLUXDB_STRATEGY_TIMEOUT` | `10` | Seconds without a strategy heartbeat before its loop is down |

**Endpoints** (same login as the rest of the API)

| Endpoint | Returns |
|----------|---------|
| `GET /metrics/health` | Whether monitoring is on, InfluxDB answers, and the watcher's last check |
| `GET /metrics/uptime` | Uptime % per bot and level |
| `GET /metrics/uptime/timeline` | Up and down periods per bot and level |
| `GET /metrics/incidents` | Down periods, labelled hang, crash or no heartbeat |
| `GET /metrics/status-changes` | What the watcher noticed: down, up again, stopped, started |
| `GET /metrics/bots` | Bots that reported a price in the last day |
| `GET /metrics/pnl` | PnL per bot and controller over time |
| `GET /metrics/orders?bot=` | One bot's price, orders (placed until filled or cancelled), fills (partial and final) and grid reference. Add `exchange=` and `pair=` for one market of a bot that trades several |
| `GET /metrics/ladder?bot=` | One bot right now: open orders, price, grid state, PnL. Takes `exchange=` and `pair=` too |
| `GET /metrics/markets`, `/metrics/feeds`, `/metrics/market` | Market recorder data: mid, spread, depth, book age |

Windows are given in minutes, up to 72 hours, the most InfluxDB 3 Core reads in one query.

**Tables** (what bots and the API write; `bot` is the bot's instance name)

| Table | Written by | Tags | Fields |
|-------|------------|------|--------|
| `bot_heartbeat` | Bot: process thread every 5 s, strategy loop every 1 s | `bot`, `level` (`process`, `strategy`) | `seen` |
| `bot_lifecycle` | Bot, when it starts and when it stops on purpose | `bot` | `event` (`started`, `stopped`) |
| `bot_price` | Bot, every second | `bot`, `exchange`, `pair` | `best_bid`, `best_ask`, `mid_price` |
| `controller_performance` | Bot, every 5 s (V2 strategies) | `bot`, `controller` | `realized_pnl_quote`, `unrealized_pnl_quote`, `global_pnl_quote`, `volume_traded`, ... |
| `grid_state` | Bot, every 5 s (grid strategies) | `bot`, `controller`, `exchange`, `pair` | `reference_price`, `levels_total`, `levels_placed` |
| `grid_level` | Bot, every 5 s (grid strategies) | `bot`, `controller`, `side`, `level` | `price`, `amount`, `state`, `order_id` |
| `open_order` | Bot, every 5 s, one row per open order | `bot`, `exchange`, `pair`, `side` | `order_id`, `price`, `amount`, `filled`, `level`, `age_s` |
| `order_event` | Bot, on each order event | `bot`, `exchange`, `pair`, `side`, `event` (`created`, `partially_filled`, `filled`, `cancelled`, `failed`, `expired`) | `order_id`, `price`, `amount`, `level`, `realized_pnl` |
| `market` | Market recorder, every second | `exchange`, `pair`, `source` | `best_bid`, `best_ask`, `mid_price`, `spread_pct`, `bid_depth_quote_1_0`, `ask_depth_quote_1_0` (also `0_5`, `2_0`), `book_age_s` |
| `bot_up` | API health watcher, every check | `bot`, `level` | `up` (1 or 0) |
| `bot_status_change` | API health watcher, on each change | `bot`, `level`, `state` (`up`, `down`, `stopped`, `started`) | `message` |

## Development

```bash
make install              # Create conda environment
conda activate hummingbot-api
make run                  # Run with hot-reload
```

## Troubleshooting

**API won't start?**
```bash
docker compose logs hummingbot-api
```

**Database issues?**
```bash
docker compose down -v    # Reset all data
make deploy               # Fresh start
```

**Check service status:**
```bash
docker ps | grep hummingbot
```

## Support

- **API Docs**: http://localhost:8000/docs
- **Issues**: https://github.com/hummingbot/hummingbot-api/issues
