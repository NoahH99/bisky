# Bisky

A modular, general-purpose Discord bot. Features live in independent
extensions (cogs), so adding one is a new module plus a config entry.

Python 3.13 · [discord.py](https://discordpy.readthedocs.io) · async SQLAlchemy 2.0 + Postgres · Alembic ·
Prometheus + Grafana · structlog · [uv](https://docs.astral.sh/uv/)

## Quick start

**1. Create the Discord application.** In the
[Developer Portal](https://discord.com/developers/applications): create an
application, open **Bot**, and copy the token. Under **Privileged Gateway
Intents** enable **Message Content Intent** — prefix commands (`!ping`) need
it. Then use **OAuth2 → URL Generator** with scopes `bot` +
`applications.commands` to invite the bot to a server.

**2. Configure.**

```sh
cp .env.example .env
# Fill in BISKY_DISCORD_TOKEN, and BISKY_DEV_GUILD_IDS=[your_server_id]
```

`BISKY_DEV_GUILD_IDS` makes slash commands appear instantly. Leave it as `[]`
and Discord does a global sync, which can take up to an hour to propagate.

**3. Run it.**

```sh
make up          # postgres, migrations, bot, Prometheus, Grafana
make logs        # follow the bot's output
make dashboards  # print the Grafana / Prometheus URLs
```

`make up-core` starts just Postgres and the bot if you don't want the
monitoring containers.

Then type `/ping` or `!ping` in your server. The reply includes the gateway
latency and a counter read back out of Postgres:

```
🏓 Pong! `42ms` — ping #1.
```

## Local development

```sh
make install                    # uv sync
make hooks                      # install the pre-commit hooks
docker compose up -d postgres   # just the database
make migrate                    # alembic upgrade head
make run                        # run the bot on the host
```

`make help` lists every target. `make check` runs exactly what CI runs:

```sh
make lint              # ruff check + ruff format --check
make typecheck         # mypy --strict
make test              # pytest with coverage
make test-integration  # tests needing a real Postgres (see below)
make fmt               # autofix and format
```

Most of the suite runs against in-memory SQLite and never touches the network.
Postgres-specific behaviour — BIGINT identity columns, `timestamptz` server
defaults, real connection pooling — is covered by a separate suite that is
skipped unless you point it at a database:

```sh
docker compose up -d postgres
make test-integration
```

## Adding a feature

Cogs are the unit of modularity. Create `src/bisky/cogs/<name>.py`:

```python
from discord.ext import commands

from bisky.bot import Bisky


class Echo(commands.Cog):
    def __init__(self, bot: Bisky) -> None:
        self.bot = bot

    @commands.hybrid_command(name="echo")  # type: ignore[arg-type]
    async def echo(self, ctx: commands.Context[Bisky], *, text: str) -> None:
        await ctx.reply(text)


async def setup(bot: commands.Bot) -> None:
    if not isinstance(bot, Bisky):
        raise TypeError("Echo requires a Bisky bot")
    await bot.add_cog(Echo(bot))
```

Then add it to `BISKY_EXTENSIONS` (JSON list) or the default in
`src/bisky/config.py`. `hybrid_command` registers both a slash command and a
prefix command from one callback. The `type: ignore` works around a
[discord.py typing gap](https://github.com/Rapptz/discord.py) in that
decorator; `commands.command` needs no ignore.

Keep the interesting logic in a plain, annotated method (as
`Ping.build_response` does) and let the command callback be a thin wrapper —
that part is testable without a gateway connection or a `Context`.

## Database changes

Edit models in `src/bisky/db/models.py`, then:

```sh
make revision m="add echo log"   # autogenerate against the running postgres
make migrate                     # apply
```

Review generated migrations before committing — autogenerate does not detect
everything (server defaults, renames, constraint changes on some backends).
CI runs `alembic check` and asserts a full `upgrade → downgrade → upgrade`
round-trip, so a migration that drifts from the models fails the build.

Surrogate keys use `BigIntPrimaryKey` from `db/base.py`: `BIGINT` on Postgres,
`INTEGER` on SQLite, because SQLite only auto-increments an
`INTEGER PRIMARY KEY`.

## Observability

A Discord bot is a long-lived outbound websocket client with no inbound
traffic, so the failures that matter are invisible to logs. `make up` brings up
Prometheus and Grafana with a provisioned dashboard at
<http://127.0.0.1:3000/d/bisky-overview>.

The bot serves three endpoints (port 8080, published to loopback only —
`/metrics` has no authentication):

| Endpoint | Meaning |
| --- | --- |
| `/healthz` | Liveness. Serving it at all proves the event loop is turning. |
| `/readyz` | Readiness: connected to the gateway and past the initial READY. 503 otherwise. |
| `/metrics` | Prometheus exposition. |

`/readyz` tracks `on_connect`/`on_resumed`/`on_disconnect` itself rather than
asking discord.py, because `Client.is_ready()` is *not* cleared on a mid-session
disconnect and `is_closed()` only reports that `close()` was called — both stay
optimistic straight through an outage.

The metrics worth knowing about:

- **`bisky_event_loop_lag_seconds`** — the one that earns its keep. A synchronous
  call in a cog (`requests`, `time.sleep`, heavy CPU) blocks heartbeats, Discord
  drops the gateway, and the bot goes quiet. In logs that looks like an
  unexplained disconnect; here it looks like what it is. Logged as a warning
  past `BISKY_EVENT_LOOP_LAG_WARN_SECONDS`.
- **`bisky_gateway_events_total{event}`** — the `connect` vs `resume` ratio.
  A reconnect that re-identifies costs part of a limited daily budget; a resume
  does not. Rising `connect` rate is the early warning.
- **`bisky_commands_total{command,kind,outcome}`** — `kind` is `prefix` or
  `slash`, so you can see how people actually invoke things.
- **`bisky_rate_limits_total{scope}`**, **`bisky_db_pool_connections{state}`**,
  **`bisky_listener_errors_total{event}`**, **`bisky_build_info`**.

**Adding metrics: label only with bounded values.** `command`, `kind`, `outcome`,
`event` are fine. A user, guild, channel or message ID is not — each distinct
value mints a permanent time series. Command names always come from
`Command.qualified_name`, never from `ctx.invoked_with`, which is arbitrary user
text and would let anyone mint labels by typing `!aaaa`, `!aaab`, …

### Logging

Logs are structlog, console-formatted locally and JSON in Docker; discord.py's
own logging is routed through the same pipeline. Every log line emitted inside a
command invocation automatically carries `command`, `kind`, `user_id` and
`guild_id`, bound once per invocation — for prefix commands in `Bisky.invoke`,
and for slash commands in `BiskyCommandTree.interaction_check`, which works
because discord.py handles each interaction in its own task.

Message content is deliberately never logged. The bot holds the Message Content
intent, so logging bodies would persist user chat and DMs.

## Configuration

Every setting is an env var prefixed `BISKY_`, read from the environment or
`.env` (see `src/bisky/config.py`). List values are JSON.

| Variable | Default | Notes |
| --- | --- | --- |
| `BISKY_DISCORD_TOKEN` | *required* | Bot token. Held as a `SecretStr`, so it stays out of logs and reprs. |
| `BISKY_DATABASE_URL` | `postgresql+asyncpg://bisky:bisky@localhost:5432/bisky` | Must use an async driver (`+asyncpg` or `+aiosqlite`). |
| `BISKY_DEV_GUILD_IDS` | `[]` | e.g. `[123456789]`. Instant slash-command sync. |
| `BISKY_OWNER_IDS` | `[]` | Who may run the admin cog. Also avoids an API call per ownership check. |
| `BISKY_EXTENSIONS` | *unset* | Unset means discover every cog. A JSON list overrides discovery. |
| `BISKY_DISABLED_EXTENSIONS` | `[]` | Module names to skip during discovery. |
| `BISKY_HTTP_PORT` | `8080` | Metrics/health port. `BISKY_HTTP_ENABLED=false` disables the server. |
| `BISKY_SYNC_COMMANDS_ON_STARTUP` | `true` | Turn off once the command tree is stable. |
| `BISKY_EVENT_LOOP_LAG_WARN_SECONDS` | `0.5` | Warn above this much loop delay. |
| `BISKY_DB_POOL_SIZE` / `BISKY_DB_MAX_OVERFLOW` | `5` / `10` | Pool ceiling is the sum. |
| `BISKY_DB_COMMAND_TIMEOUT` | `30.0` | Without it, one wedged query hangs a command forever. |
| `BISKY_COMMAND_PREFIX` | `!` | The bot also responds to @mentions. |
| `BISKY_LOG_LEVEL` | `INFO` | |
| `BISKY_LOG_FORMAT` | `console` | `json` in Docker. |
| `BISKY_DB_ECHO` | `false` | Log every SQL statement. |

A missing or invalid setting fails at startup with a message naming the
variable, rather than at first use.

## Layout

```
src/bisky/
  __main__.py       entrypoint: settings → logging → signals → database → bot
  bot.py            Bisky(commands.Bot) and BiskyCommandTree
  config.py         pydantic-settings Settings
  logging.py        structlog, console or JSON, stdlib logging routed through it
  metrics.py        Prometheus metric definitions and the loop-lag monitor
  health.py         /healthz, /readyz, /metrics on aiohttp; GatewayState
  observability.py  command and gateway listeners (a module, not a cog — see below)
  cogs/ping.py      example feature
  cogs/admin.py     owner-only reload/load/unload/sync
  db/
    base.py         DeclarativeBase, naming convention, BigIntPrimaryKey
    models.py       ORM models
    repository.py   query helpers, tested directly
    session.py      Database: engine + session context manager
migrations/         Alembic
docker/             Prometheus config, Grafana provisioning and dashboard
tests/              pytest, async by default
```

`observability.py` is a plain module with a `register(bot)` function rather than
a cog, on purpose: the admin cog can unload cogs at runtime, and losing all
metrics to a stray `!unload observability` would be a bad trade. discord.py also
unloads every cog *before* closing the websocket, so a cog would miss the final
disconnect.

## Testing

Tests run against in-memory SQLite with `StaticPool`, so the suite needs no
services and each test gets a fresh database. Postgres-specific behaviour is
covered by the migrations job in CI.

Discord is never contacted: the command-sync tests monkeypatch
`CommandTree.sync`, and cog tests pass stub bots and contexts.

Settings are isolated by an autouse fixture that unsets `BISKY_*` and detaches
the `.env` file. Without it a filled-in `.env` feeds your real token and guild
IDs into the suite, and tests pass or fail depending on whose checkout they run
in.

Metrics live on Prometheus' global default registry, so values carry across
tests. Assertions compare **deltas** via `tests/helpers.py::sample`, never
absolute totals.

## CI

`.github/workflows/ci.yml` runs four jobs in parallel: **lint** (ruff +
mypy --strict), **test** (pytest, coverage artifact, pre-commit), **migrations**
(round-trip and drift check plus the integration suite against real Postgres),
and **docker** (image builds with layer caching).

## Deployment notes

The image runs as a non-root user (`bisky`, uid 1001) and installs the package
non-editable, so the runtime stage carries only the virtualenv, `alembic.ini`
and `migrations/`. The `migrate` compose service runs to completion before
`bot` starts, so schema changes are applied exactly once per deploy.

The bot installs its own SIGTERM handler. This is load-bearing, not tidiness:
the container runs it as PID 1, and PID 1 ignores signals that have no explicit
handler — so without it `docker stop` waits out the grace period and then
SIGKILLs, leaving the gateway session open and the pool undisposed.

The `bot` service has a healthcheck against `/readyz`. Be aware that plain
`docker compose` does **not** restart an unhealthy container; it only reports
status and gates `depends_on`. The healthcheck is for visibility.

The healthcheck probes with `python -c urllib...` rather than curl, because
`python:slim` doesn't ship curl.

Both `uv` and the Python base image are pinned in the `Dockerfile`; bump
`UV_VERSION` / `PYTHON_VERSION` there and in `.github/workflows/ci.yml`.
