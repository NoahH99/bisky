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
make up      # postgres, migrations, bot, Prometheus, Grafana, pgAdmin
make logs    # follow the bot's output
make urls    # print every local web UI
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

## Aura (the economy)

> Full derivations — RTP proofs, variance, expected values, progression maths
> and the inflation model — are in **[ECONOMY.md](ECONOMY.md)**. This section is
> the summary.

An opt-in per-guild economy. `economy` is a normal feature cog, so it is **off
everywhere until enabled**: `!cogs enable economy <guild_id>`.

You earn **aura** by talking and by sitting in voice. Aura always lands in your
**wallet**, which is spendable; moving it to the **bank** costs a fee but keeps
it safe. Roles are bought from the wallet, so banking has a real cost.

```
!balance [member]      # wallet, bank, net worth, lifetime earned
!work                  # once a day, 100-200 aura
!pay @user 500         # send aura, minus a 5% tax
!deposit 500 | all     # wallet -> bank, minus the fee
!withdraw 500 | all    # bank -> wallet, free
!leaderboard           # richest members, plus total aura in circulation
!shop                  # roles for sale
!buy @Tier I           # buy one with wallet aura
```

Admin (Administrator, or any global admin):

```
!economy                                  # show tuning and aura in circulation
!economy set voice_aura_per_minute 2
!economy role add @Tier I 250 1           # tier 1 on the ladder
!economy role add @Cosmetic 99            # no tier = standalone
!economy role remove @Cosmetic
```

### Games

`gambling` is a **separate cog** from `economy`, so a server can run the economy
without a casino. It requires `economy` to be enabled too — a casino with no way
to earn a stake is not much use.

```
!coinflip 500 heads    # 1.95x
!slots 500             # three of a kind 11x, a pair 1.5x
!roulette 500 red      # or black / even / odd / a number 0-36
!dice 500 over 60      # pick your own odds; longer odds pay more
!rob @user             # 35% to take a quarter of their wallet
!duel @user 500        # both stake, winner takes the pot minus rake
!lottery               # pot, your tickets, your odds
!lottery buy 5
!lottery draw          # admin
```

Every payout is integer aura and every game takes an injected `random.Random`,
so the odds are tested by **enumerating the whole outcome space** rather than
sampling. Measured returns to player:

| Game | RTP | House edge |
| --- | --- | --- |
| Coinflip | 0.9750 | 2.5% |
| Roulette (any bet) | 0.9730 | 2.7% |
| Dice (any target) | 0.9700 | 3.0% |
| Slots | 0.9306 | 6.9% |

See [ECONOMY.md §5](ECONOMY.md) for the derivations and variance figures.

Roulette is a European single-zero wheel, so zero losing every outside bet is
*where the edge comes from*. Dice pays proportionally more for longer odds, so
picking a riskier target buys variance rather than a better deal — the edge is
flat at 3% whatever you choose. `test_games.py` asserts all of this; a game that
drifts to RTP ≥ 1 is a money printer, and that is the failure those tests exist
to catch.

Winnings are credited with `count_as_earned=False`. They are recycled aura, not
income, so they never inflate the lifetime-earned figure the pacing model uses.

Robbery is deliberately unattractive: 35% success, takes a quarter of the
target's wallet, 4-hour cooldown, and a 10% fine on failure. Without a cost,
robbery is a free roll and the optimal play is spamming it at everyone forever.
Robbing someone with an almost-empty wallet refunds the cooldown rather than
wasting it. Only wallets are robbable — that is what the bank is for.

### Earning, and why the idle rules matter

Defaults: **1 aura per minute of voice**, **2 aura per message** on a 60-second
per-user cooldown, and **100-200 from `!work`** once a day. Voice only pays when
all three hold:

- **≥2 non-bot humans** in the channel — bots do not count as company
- you are **not self-deafened** — the one signal that reliably means "not here"
- the channel is **not the guild's AFK channel**

These are load-bearing, not cosmetic. Without them an overnight AFK session
mints 480 aura, which is two tier-1 roles earned while asleep.

Voice is credited by a **once-a-minute tick** rather than on disconnect, so a
crash costs at most a minute instead of a whole session.

#### Why `!work` exists

It is a floor, not a bonus. On voice income alone the ladder takes 5.7 years at
6h/day — but **34 years at 1h/day**, so without a daily claim it is only
finishable by the single most active person in the server. At 100–200/day it
becomes ~4 years at 6h/day and ~10 years at 1h/day, which keeps voice clearly
the main path while making the ladder reachable at all for everyone else.

#### Where cooldowns live

Split deliberately, and the rule is *"is the cooldown longer than the gap
between restarts?"*:

- **In memory** — the 60-second message cooldown. A restart costs someone two
  aura, which is not worth a database read on every single message.
- **On disk** (`economy_cooldowns`) — `!work` and `!rob`. A daily cooldown held
  in memory would reset on every deploy and hand out a free claim each time.

Claiming a persisted cooldown is a conditional UPDATE for the same reason debits
are, so two racing invocations cannot both succeed.

### The ladder, and the five-year target

There are two shop types. **Ladder** roles carry a tier and must be bought in
order; **standalone** roles have no tier and can be bought in any order. The
ladder is priced against a specific target: someone in voice ~6 h/day should
reach tier 7 in about five years.

| Tier | Price | Cumulative | Voice hours | Time @ 6h/day |
| --- | --- | --- | --- | --- |
| 1 | 250 | 250 | 4 | ~1 day |
| 2 | 1,000 | 1,250 | 21 | ~4 days |
| 3 | 4,000 | 5,250 | 88 | ~2 weeks |
| 4 | 15,000 | 20,250 | 338 | ~2 months |
| 5 | 55,000 | 75,250 | 1,254 | ~7 months |
| 6 | 175,000 | 250,250 | 4,171 | ~1.9 years |
| 7 | 500,000 | 750,250 | 12,504 | **~5.7 years** |

Each tier costs roughly three times the *cumulative* time of everything before
it, so the unit of progress shifts from days to weeks to months to years. Tier 1
lands in an evening; tier 7 is a genuine long haul. `test_economy_rules.py` pins
this pacing, so a future price tweak cannot silently turn five years into five
weeks.

Prices are per-guild rows, not code — retune without a deploy.

### Keeping inflation down

Income is a flat rate, so supply grows **linearly**, which is very
controllable. The sinks, in order of how much they matter:

1. **Role purchases** — dominant by construction; the ladder is priced to absorb
   roughly a lifetime of income.
2. **The 5% deposit fee** — burned outright, not paid to anyone. The only sink
   that touches every player regardless of how they play, and the thing that
   makes wallet-versus-bank an actual decision.
3. **Gambling house edge** — 2.5–6.9% of everything wagered, depending on game.
4. **The lottery rake** — 10% of every pot. The only sink here that never runs
   out, which is what makes it the answer to the endgame problem below.
5. **Failed-robbery fines, duel rake, transfer tax** — smaller, but they all
   burn rather than redistribute.

`bisky_economy_minted_total{source}` and `bisky_economy_burned_total{sink}` are
exported, so you can graph minted-minus-burned and retune prices from data
rather than by feel.

**Known gap:** the ladder is a *finite* sink. Once someone owns tier 7 their
balance grows with nothing to spend it on. Standalone roles delay that but are
also finite; repeatable sinks are the real answer and are deliberately not
guessed at yet.

### Correctness notes

Aura is an **integer everywhere**. Currency in floating point accumulates
rounding error that shows up as money quietly appearing or vanishing.

Every debit is a **conditional UPDATE**, not a read-modify-write:

```sql
UPDATE economy_accounts SET wallet = wallet - :amount
 WHERE guild_id = ... AND user_id = ... AND wallet >= :amount
```

Two commands racing on the same wallet would otherwise both read the old
balance and both succeed, spending the same aura twice. The guard lives in the
WHERE clause so the database arbitrates — no explicit locking, and it behaves
identically on Postgres and on the SQLite the tests use. There is a test that
runs two concurrent full-balance debits and asserts exactly one wins.

`CHECK` constraints on `wallet >= 0` and `bank >= 0` are the backstop if a
future code path ever forgets.

Routine earning is **not** written to the transaction log — a row per voice
minute per user would add millions of rows a year. `lifetime_earned` and the
Prometheus counters cover it in aggregate; the log records notable movements.

## Help

`!help` lists every command available *to you, here* — commands from a cog that
is disabled in this guild are omitted rather than advertised and then refused,
because `HelpCommand` runs each command's checks before listing it and the cog
gate is a global check.

```
!help              # everything, grouped by cog
!help economy      # a group and its subcommands
!economy help      # the same thing, which is what people actually type
!help coinflip     # usage, description, aliases
```

That second form needs explaining. discord.py only understands `!help <group>`:
with `invoke_without_command=True` the trailing word is handed to the group
callback and silently ignored, so `!economy help` just printed the economy
settings. `attach_group_help` in `help.py` registers a real `help` subcommand on
every group at startup, which fixes it once rather than each group having to
remember. Those helpers inherit their parent's cog, so `!admin help` still goes
through the owner check instead of leaking the admin command structure.

Slash users mostly do not need this: Discord's own command picker shows the
description of every `/` command, which is why the descriptions on hybrid
commands are written to stand alone.

## Permissions

Two ladders, and global admin outranks guild admin.

**Global admins** may run bot-wide commands (`!reload`, `!sync`, `!admin ...`)
and may also act as a guild admin in *any* guild, so you can fix a server's
configuration without being granted permissions there.

Membership comes from two places, and this matters:

- The **application owner is always a global admin**, checked against Discord
  rather than the database. That is the anti-lockout guarantee — no `!admin
  remove`, and no lost table, can strand you outside the bot.
- Everyone else is a row in `global_admins`, managed with `!admin add @user` /
  `!admin remove @user` / `!admin list`.

`BISKY_GLOBAL_ADMIN_IDS` seeds grants at startup. Seeding is **additive and
never authoritative**: it will not revoke an admin who is missing from the
variable, because otherwise a deploy with the variable unset would silently
wipe your admin list.

**Guild admins** are anyone holding Discord's Administrator permission in that
guild. They can change that guild's settings and nothing else.

Both admin cogs are prefix-only, deliberately. `!sync` republishes the slash
command tree, so making it a slash command would be circular — the command that
repairs the tree cannot depend on the tree. Slash commands are also visible to
every member of a guild, and Discord gates them on *guild* permissions, which
cannot express "only this bot's admins".

## Per-guild cogs

Every cog is **off in a guild until someone turns it on**, except the core cogs
(`global_admin`, `guild_admin`), which are always on everywhere. So a server the
bot has just joined has no feature commands at all until a global admin enables
them.

```
!cogs                          # what's enabled here
!cogs 123456789                # ...or in another guild
!cogs enable economy           # here
!cogs enable economy 123456789 # in a specific guild, from anywhere
!cogs disable economy 123456789
!cogs off economy 123456789    # `on`/`off` are aliases
!cogs where economy            # every guild it's enabled in
```

Only global admins can toggle, and the guild is just an argument, so you can
administer a server you are not in — or from a DM.

Note the vocabulary split, because it is the confusing part:

- **`!extensions`** is process-wide. discord.py loads cogs into the *bot*, not
  into a guild (`Bot.__cogs` and `Bot.__extensions` are plain dicts), so
  `!unload` removes a cog everywhere at once. This is a development tool.
- **`!cogs`** is per-guild enablement, which is what you almost always want.

### How the gate works

Since a cog cannot be loaded per-guild, cogs load once and a check runs before
any of their commands:

- **Prefix and hybrid commands** go through a global check registered with
  `bot.add_check(...)`. It is deliberately **not** `call_once`: those checks run
  only in `BotBase.invoke`, which hybrid commands invoked as slash commands
  never reach — they call `Command.prepare` directly, which consults the default
  check list. Registering on the wrong list silently leaves slash usage ungated.
- **Pure application commands** never touch that path, so they are gated in
  `BiskyCommandTree.interaction_check`.

`CogDisabled` inherits from *both* `commands.CheckFailure` and
`app_commands.CheckFailure`, so one exception is classified as a user error by
either error handler.

A disabled cog's **slash commands still appear** in the guild's command picker
and are rejected on use — the command tree is synced globally. Making them
invisible would mean maintaining a per-guild tree and re-syncing on every
toggle, against a per-guild daily overwrite limit. The check is required either
way, since prefix commands bypass the tree entirely.

Feature cogs are also refused in DMs: there is no guild to be enabled in, so
defaulting to "on" there would be a hole. Core cogs still work in DMs, which is
what makes recovery possible from anywhere.

Which cogs are core lives in `CORE_COGS` in `guild_cogs.py`, listed centrally
rather than declared by each module: it is a permission decision, so it should
be auditable in one place and not something a new cog can opt itself into.

## Prefixes

Each guild can set its own prefix with `!prefix set ?`, view it with `!prefix`,
and restore the default with `!prefix reset`. Requires Administrator (or global
admin).

**Mentions always work**, regardless of prefix. That is the way back in if
someone sets an unusual prefix and forgets it: `@Bisky prefix` still answers.

`command_prefix` is evaluated for **every message the bot can see**, so it is
the hottest path in the process and must never touch the database. `prefix.py`
serves overrides from an in-memory cache, primes it on write, and evicts on
guild removal. Absent overrides are cached too — a guild with no override is the
common case, and not caching it would mean a query per message.
`bisky_prefix_cache_total{result}` reports hits versus misses; a rising miss
rate means something is invalidating too eagerly.

Prefixes are validated: 1-8 characters, no whitespace, and may not start with
`/`, `@`, `#` or a mention.

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
`INTEGER PRIMARY KEY`. Discord IDs are always `BigInteger` — snowflakes exceed
32 bits — and where one *is* the primary key it is a natural key supplied by
us, so `autoincrement=False`.

Settings tables use nullable columns where null means "inherit the default".
That is what lets `!prefix reset` restore default behaviour by nulling the
column rather than deleting the row and taking any other settings with it.

## Browsing the database

pgAdmin runs at <http://127.0.0.1:5050> with the `bisky` server already
registered, so you only need to enter the password (`POSTGRES_PASSWORD`,
default `bisky`) the first time you expand it.

It runs in **desktop mode** with the master password disabled, so there is no
login screen. That is only acceptable because, like every other UI here, it is
published to loopback and nothing else.

Two things that will bite otherwise:

- **The email must use a real-looking domain.** pgAdmin validates
  `PGADMIN_DEFAULT_EMAIL` and rejects reserved TLDs such as `.local`, then
  crash-loops on startup with a validation error rather than a clear one.
- **`servers.json` is imported only on first start**, while the `pgadmin-data`
  volume is empty. If you change `docker/pgadmin/servers.json`, run
  `docker volume rm bisky_pgadmin-data` to make it take effect again.

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
| `BISKY_OWNER_IDS` | `[]` | Application owner(s). Always implicitly global admins. Also avoids an API call per ownership check. |
| `BISKY_GLOBAL_ADMIN_IDS` | `[]` | Granted global admin at startup, additively. Never revokes. |
| `BISKY_EXTENSIONS` | *unset* | Unset means discover every cog. A JSON list overrides discovery. |
| `BISKY_DISABLED_EXTENSIONS` | `[]` | Module names to skip during discovery. |
| `BISKY_HTTP_PORT` | `8080` | Metrics/health port. `BISKY_HTTP_ENABLED=false` disables the server. |
| `BISKY_SYNC_COMMANDS_ON_STARTUP` | `true` | Turn off once the command tree is stable. |
| `BISKY_EVENT_LOOP_LAG_WARN_SECONDS` | `0.5` | Warn above this much loop delay. |
| `BISKY_DB_POOL_SIZE` / `BISKY_DB_MAX_OVERFLOW` | `5` / `10` | Pool ceiling is the sum. |
| `BISKY_DB_COMMAND_TIMEOUT` | `30.0` | Without it, one wedged query hangs a command forever. |
| `BISKY_COMMAND_PREFIX` | `!` | Default prefix; guilds can override it. The bot also always responds to @mentions. |
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
  checks.py         permission ladders: global admin, guild admin
  prefix.py         per-guild prefix cache and validation
  economy.py        aura rules: rates, fees, ladder maths (pure functions)
  cogs/ping.py      example feature
  games.py          game odds and payouts (pure functions, exact RTP)
  cogs/economy.py   aura: earning, banking, role shop
  cogs/gambling.py  casino games, robbery, duels, lottery
  guild_cogs.py     per-guild cog enablement: core list, cache, gate helpers
  cogs/global_admin.py  bot-wide: extensions, per-guild cogs, manage admins
  cogs/guild_admin.py   per-guild: prefix show/set/reset
  db/
    base.py         DeclarativeBase, naming convention, timestamp mixins
    models.py       ORM models
    repository/     query helpers by area, tested directly
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
