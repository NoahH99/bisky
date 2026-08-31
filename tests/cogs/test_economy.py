"""Tests for the economy cog.

Two things here matter more than the rest: passive earning must respect
per-guild enablement (it bypasses the command check entirely), and the hot
paths must not query on every message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import discord
import pytest
from discord.ext import commands

from bisky.bot import Bisky
from bisky.checks import NotGlobalAdmin, NotGuildAdmin
from bisky.cogs.economy import ALL, Economy, parse_amount, setup
from bisky.config import Settings
from bisky.db.repository import economy as repo
from bisky.db.repository import enable_cog
from bisky.db.session import Database
from tests.helpers import sample

GUILD = 3000
USER = 4000


@dataclass
class StubAuthor:
    id: int = USER
    bot: bool = False
    display_name: str = "someone"


@dataclass
class StubMessage:
    author: StubAuthor = field(default_factory=StubAuthor)
    guild_id: int | None = GUILD

    @property
    def guild(self) -> Any:
        if self.guild_id is None:
            return None
        return type("G", (), {"id": self.guild_id})()


class StubGuild:
    """Stands in for a Guild in command tests.

    ``get_member`` and ``get_role`` return None deliberately: a role can be
    deleted in Discord while still listed in the shop, so the cog has to cope
    with lookups that fail.
    """

    def __init__(self, guild_id: int) -> None:
        self.id = guild_id

    def get_member(self, _user_id: int) -> None:
        return None

    def get_role(self, _role_id: int) -> None:
        return None


@dataclass
class StubContext:
    author_id: int = USER
    guild_id: int | None = GUILD
    replies: list[str] = field(default_factory=list)

    @property
    def author(self) -> Any:
        return type("A", (), {"id": self.author_id, "display_name": "someone"})()

    @property
    def guild(self) -> Any:
        if self.guild_id is None:
            return None
        return StubGuild(self.guild_id)

    async def reply(self, content: str) -> None:
        self.replies.append(content)


@pytest.fixture
async def bot(settings: Settings, database: Database) -> Bisky:
    bot = Bisky(settings, database)
    # The economy cog is off by default, like any non-core cog.
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")
    return bot


@pytest.fixture
def cog(bot: Bisky) -> Economy:
    # Constructed directly rather than via cog_load, which would start the
    # voice tick loop.
    return Economy(bot)


async def run(cog: Economy, command: str, ctx: StubContext, *args: Any) -> None:
    callback = cast(Any, getattr(type(cog), command)).callback
    await callback(cog, ctx, *args)


async def wallet_of(database: Database, user_id: int = USER) -> int:
    async with database.session() as session:
        return (await repo.get_account(session, GUILD, user_id)).wallet


# -- amount parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("100", 100), ("1,000", 1000), ("1_000", 1000), ("all", ALL), ("MAX", ALL)],
)
def test_parse_amount(text: str, expected: int) -> None:
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["abc", "-5", "0", "1.5", ""])
def test_parse_amount_rejects_nonsense(text: str) -> None:
    with pytest.raises(commands.BadArgument):
        parse_amount(text)


# -- message earning ---------------------------------------------------------


async def test_message_earns_aura(cog: Economy, database: Database) -> None:
    minted = sample("bisky_economy_minted_total", source="message")

    await cog.on_message(cast(Any, StubMessage()))

    assert await wallet_of(database) == 2
    assert sample("bisky_economy_minted_total", source="message") == minted + 2


async def test_second_message_is_on_cooldown(cog: Economy, database: Database) -> None:
    await cog.on_message(cast(Any, StubMessage()))
    await cog.on_message(cast(Any, StubMessage()))

    assert await wallet_of(database) == 2


async def test_bots_earn_nothing(cog: Economy, database: Database) -> None:
    await cog.on_message(cast(Any, StubMessage(author=StubAuthor(bot=True))))

    assert await wallet_of(database) == 0


async def test_dms_earn_nothing(cog: Economy, database: Database) -> None:
    await cog.on_message(cast(Any, StubMessage(guild_id=None)))

    assert await wallet_of(database) == 0


async def test_disabled_cog_earns_nothing(settings: Settings, database: Database) -> None:
    """Passive earning bypasses the command check, so it must check itself."""
    cog = Economy(Bisky(settings, database))

    await cog.on_message(cast(Any, StubMessage(guild_id=9999)))

    async with database.session() as session:
        assert (await repo.get_account(session, 9999, USER)).wallet == 0


async def test_message_earning_honours_configured_rate(cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.set_rules(session, GUILD, message_aura=7)
    cog.forget_rules(GUILD)

    await cog.on_message(cast(Any, StubMessage()))

    assert await wallet_of(database) == 7


async def test_message_earning_can_be_switched_off(cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.set_rules(session, GUILD, message_aura=0)
    cog.forget_rules(GUILD)

    await cog.on_message(cast(Any, StubMessage()))

    assert await wallet_of(database) == 0


# -- voice earning -----------------------------------------------------------


def voice_member(user_id: int, *, deaf: bool = False, is_bot: bool = False) -> Any:
    voice = type("V", (), {"self_deaf": deaf, "deaf": False})()
    return type("M", (), {"id": user_id, "bot": is_bot, "voice": voice})()


def voice_guild(members: list[Any], *, afk: bool = False) -> Any:
    channel = type("C", (), {"members": members})()
    afk_channel = channel if afk else None
    return type(
        "G",
        (),
        {"id": GUILD, "voice_channels": [channel], "afk_channel": afk_channel},
    )()


async def test_voice_credits_a_populated_channel(cog: Economy, database: Database) -> None:
    guild = voice_guild([voice_member(USER), voice_member(USER + 1)])
    minted = sample("bisky_economy_minted_total", source="voice")

    credited = await cog._credit_guild_voice(guild, await cog.rules_for(GUILD))

    assert credited == 2
    assert await wallet_of(database) == 1
    assert sample("bisky_economy_minted_total", source="voice") == minted + 2


async def test_sitting_alone_earns_nothing(cog: Economy, database: Database) -> None:
    guild = voice_guild([voice_member(USER)])

    assert await cog._credit_guild_voice(guild, await cog.rules_for(GUILD)) == 0
    assert await wallet_of(database) == 0


async def test_bots_do_not_count_towards_company(cog: Economy, database: Database) -> None:
    """A user alone with the bot is still alone."""
    guild = voice_guild([voice_member(USER), voice_member(USER + 1, is_bot=True)])

    assert await cog._credit_guild_voice(guild, await cog.rules_for(GUILD)) == 0


async def test_self_deafened_member_is_skipped(cog: Economy, database: Database) -> None:
    guild = voice_guild([voice_member(USER, deaf=True), voice_member(USER + 1)])

    credited = await cog._credit_guild_voice(guild, await cog.rules_for(GUILD))

    assert credited == 1
    assert await wallet_of(database) == 0
    assert await wallet_of(database, USER + 1) == 1


async def test_afk_channel_earns_nothing(cog: Economy, database: Database) -> None:
    guild = voice_guild([voice_member(USER), voice_member(USER + 1)], afk=True)

    assert await cog._credit_guild_voice(guild, await cog.rules_for(GUILD)) == 0


# -- banking -----------------------------------------------------------------


async def test_deposit_takes_a_fee(cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 1_000)
    burned = sample("bisky_economy_burned_total", sink="deposit_fee")
    ctx = StubContext()

    await run(cog, "deposit", ctx, "1000")

    async with database.session() as session:
        account = await repo.get_account(session, GUILD, USER)
    assert (account.wallet, account.bank) == (0, 950)
    assert sample("bisky_economy_burned_total", sink="deposit_fee") == burned + 50
    assert "950" in ctx.replies[0]


async def test_deposit_all_uses_the_whole_wallet(cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 200)
    ctx = StubContext()

    await run(cog, "deposit", ctx, "all")

    async with database.session() as session:
        assert (await repo.get_account(session, GUILD, USER)).bank == 190


async def test_deposit_with_empty_wallet(cog: Economy) -> None:
    ctx = StubContext()

    await run(cog, "deposit", ctx, "all")

    assert "empty" in ctx.replies[0]


async def test_withdraw_is_free(cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 100)
        await repo.move_to_bank(session, GUILD, USER, 100, fee=0)
    ctx = StubContext()

    await run(cog, "withdraw", ctx, "all")

    async with database.session() as session:
        account = await repo.get_account(session, GUILD, USER)
    assert (account.wallet, account.bank) == (100, 0)


async def test_balance_shows_both_pots(cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 1_500)
    ctx = StubContext()

    await run(cog, "balance", ctx, None)

    assert "1,500" in ctx.replies[0]


async def test_leaderboard_when_empty(cog: Economy) -> None:
    ctx = StubContext()

    await run(cog, "leaderboard", ctx)

    assert "Nobody" in ctx.replies[0]


# -- role shop ---------------------------------------------------------------


def stub_role(role_id: int, name: str = "Tier I") -> Any:
    return cast(Any, type("R", (), {"id": role_id, "name": name})())


async def test_shop_is_empty_initially(cog: Economy) -> None:
    ctx = StubContext()

    await run(cog, "shop", ctx)

    assert "empty" in ctx.replies[0]


async def test_buying_an_unlisted_role_is_refused(cog: Economy) -> None:
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(10))

    assert "not for sale" in ctx.replies[0]


async def test_setup_rejects_a_plain_bot() -> None:
    with pytest.raises(TypeError, match="requires a Bisky bot"):
        await setup(cast(Any, object()))


async def add_reward(database: Database, role_id: int, price: int, tier: int | None) -> None:
    async with database.session() as session:
        await repo.upsert_role_reward(session, GUILD, role_id, price=price, tier=tier)


async def test_buying_without_enough_aura_is_refused(cog: Economy, database: Database) -> None:
    await add_reward(database, 10, 250, 1)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 100)
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(10))

    assert "costs" in ctx.replies[0]
    assert await wallet_of(database) == 100  # nothing taken


async def test_bank_balance_does_not_pay_for_roles(cog: Economy, database: Database) -> None:
    """Roles are bought from the wallet, so banking has a real cost."""
    await add_reward(database, 10, 250, 1)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 300)
        await repo.move_to_bank(session, GUILD, USER, 300, fee=0)
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(10))

    assert "Withdraw from the bank" in ctx.replies[0]


async def test_higher_tier_is_locked_until_the_one_below_is_owned(
    cog: Economy, database: Database
) -> None:
    await add_reward(database, 10, 250, 1)
    await add_reward(database, 20, 1_000, 2)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 5_000)
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(20, "Tier II"))

    assert "tier 1 first" in ctx.replies[0]
    assert await wallet_of(database) == 5_000


async def test_buying_the_unlocked_tier_debits_and_records(
    cog: Economy, database: Database
) -> None:
    await add_reward(database, 10, 250, 1)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 1_000)
    burned = sample("bisky_economy_burned_total", sink="role_purchase")
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(10))

    assert await wallet_of(database) == 750
    async with database.session() as session:
        assert await repo.owned_tiers(session, GUILD, USER) == {1}
    assert sample("bisky_economy_burned_total", sink="role_purchase") == burned + 250


async def test_buying_the_same_role_twice_is_refused(cog: Economy, database: Database) -> None:
    await add_reward(database, 10, 250, 1)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 1_000)
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(10))
    await run(cog, "buy", ctx, stub_role(10))

    assert "already own" in ctx.replies[1]
    assert await wallet_of(database) == 750  # charged once


async def test_standalone_roles_need_no_ladder_progress(cog: Economy, database: Database) -> None:
    await add_reward(database, 30, 99, None)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 200)
    ctx = StubContext()

    await run(cog, "buy", ctx, stub_role(30, "Cosmetic"))

    assert await wallet_of(database) == 101


async def test_shop_marks_owned_unlocked_and_locked(cog: Economy, database: Database) -> None:
    await add_reward(database, 10, 250, 1)
    await add_reward(database, 20, 1_000, 2)
    await add_reward(database, 30, 99, None)
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, USER, 1_000)
    ctx = StubContext()
    await run(cog, "buy", ctx, stub_role(10))

    await run(cog, "shop", ctx)

    body = ctx.replies[-1]
    assert "✅" in body  # tier 1, owned
    assert "🛒" in body  # tier 2, now unlocked
    assert "Standalone" in body


async def test_economy_set_changes_a_rule_and_clears_the_cache(
    cog: Economy, database: Database
) -> None:
    ctx = StubContext()
    await cog.rules_for(GUILD)  # prime the cache

    await run(cog, "economy_set", ctx, "voice_aura_per_minute", 5)

    assert (await cog.rules_for(GUILD)).voice_aura_per_minute == 5


async def test_economy_set_rejects_unknown_fields(cog: Economy) -> None:
    ctx = StubContext()

    with pytest.raises(commands.BadArgument, match="Unknown setting"):
        await run(cog, "economy_set", ctx, "nonsense", 1)


async def test_economy_set_enforces_floors(cog: Economy) -> None:
    ctx = StubContext()

    with pytest.raises(commands.BadArgument, match="cannot be below"):
        await run(cog, "economy_set", ctx, "min_voice_humans", 0)


async def test_voice_tick_survives_a_client_that_never_logged_in(
    settings: Settings, database: Database
) -> None:
    """wait_until_ready raises off-gateway; an unguarded raise kills the loop.

    Loading the cog outside a live session must not leave a dead voice tick,
    or voice earning would stop with nothing but an unretrieved-task warning.
    """
    cog = Economy(Bisky(settings, database))
    await cog.cog_load()
    try:
        await cog.before_voice_tick()  # must not raise
        assert not cog.voice_tick.failed()
    finally:
        await cog.cog_unload()


async def test_voice_tick_is_a_noop_without_guilds(cog: Economy) -> None:
    await cog.voice_tick()


# -- global admin commands ---------------------------------------------------


@pytest.fixture
async def admin_bot(settings: Settings, database: Database) -> Bisky:
    """A bot where USER is the application owner, so counts as a global admin."""
    bot = Bisky(settings.model_copy(update={"owner_ids": [USER]}), database)
    async with database.session() as session:
        await enable_cog(session, GUILD, "economy")
    return bot


@pytest.fixture
def admin_cog(admin_bot: Bisky) -> Economy:
    return Economy(admin_bot)


def target(user_id: int = 5555) -> Any:
    return cast(Any, type("M", (), {"id": user_id, "display_name": "target", "bot": False})())


async def test_give_mints_into_the_wallet(admin_cog: Economy, database: Database) -> None:
    minted = sample("bisky_economy_minted_total", source="admin")
    ctx = StubContext()

    await run(admin_cog, "economy_give", ctx, target(), 500, "wallet")

    assert await wallet_of(database, 5555) == 500
    assert sample("bisky_economy_minted_total", source="admin") == minted + 500


async def test_give_can_target_the_bank(admin_cog: Economy, database: Database) -> None:
    """Bypasses the deposit fee; only admin grants may do that."""
    ctx = StubContext()

    await run(admin_cog, "economy_give", ctx, target(), 1_000, "bank")

    async with database.session() as session:
        account = await repo.get_account(session, GUILD, 5555)
    assert (account.wallet, account.bank) == (0, 1_000)


async def test_granted_aura_is_not_lifetime_earned(admin_cog: Economy, database: Database) -> None:
    """It was not earned, and counting it would distort the pacing model."""
    await run(admin_cog, "economy_give", StubContext(), target(), 900, "wallet")

    async with database.session() as session:
        assert (await repo.get_account(session, GUILD, 5555)).lifetime_earned == 0


async def test_give_defaults_to_the_wallet(admin_cog: Economy, database: Database) -> None:
    await run(admin_cog, "economy_give", StubContext(), target(), 250)

    assert await wallet_of(database, 5555) == 250


@pytest.mark.parametrize("amount", [0, -100])
async def test_give_rejects_non_positive_amounts(admin_cog: Economy, amount: int) -> None:
    with pytest.raises(commands.BadArgument, match="greater than zero"):
        await run(admin_cog, "economy_give", StubContext(), target(), amount, "wallet")


async def test_give_rejects_an_unknown_destination(admin_cog: Economy) -> None:
    with pytest.raises(commands.BadArgument, match="wallet"):
        await run(admin_cog, "economy_give", StubContext(), target(), 100, "pocket")


async def test_take_burns_from_the_wallet(admin_cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, 5555, 1_000)
    burned = sample("bisky_economy_burned_total", sink="admin")

    await run(admin_cog, "economy_take", StubContext(), target(), 400, "wallet")

    assert await wallet_of(database, 5555) == 600
    assert sample("bisky_economy_burned_total", sink="admin") == burned + 400


async def test_take_burns_from_the_bank(admin_cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_bank(session, GUILD, 5555, 800)

    await run(admin_cog, "economy_take", StubContext(), target(), 300, "bank")

    async with database.session() as session:
        assert (await repo.get_account(session, GUILD, 5555)).bank == 500


async def test_take_refuses_to_overdraw(admin_cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, 5555, 100)
    ctx = StubContext()

    await run(admin_cog, "economy_take", ctx, target(), 500, "wallet")

    assert "only holds" in ctx.replies[0]
    assert await wallet_of(database, 5555) == 100


async def test_reset_zeroes_both_pots(admin_cog: Economy, database: Database) -> None:
    async with database.session() as session:
        await repo.credit_wallet(session, GUILD, 5555, 700)
        await repo.credit_bank(session, GUILD, 5555, 300)
    burned = sample("bisky_economy_burned_total", sink="admin")
    ctx = StubContext()

    await run(admin_cog, "economy_reset", ctx, target())

    async with database.session() as session:
        account = await repo.get_account(session, GUILD, 5555)
    assert (account.wallet, account.bank) == (0, 0)
    assert sample("bisky_economy_burned_total", sink="admin") == burned + 1_000
    assert "1,000" in ctx.replies[0]


async def test_reset_of_an_empty_account_is_harmless(admin_cog: Economy) -> None:
    ctx = StubContext()

    await run(admin_cog, "economy_reset", ctx, target())

    assert "0" in ctx.replies[0]


async def test_admin_actions_are_written_to_the_audit_log(
    admin_cog: Economy, database: Database
) -> None:
    await run(admin_cog, "economy_give", StubContext(), target(), 500, "wallet")
    await run(admin_cog, "economy_take", StubContext(), target(), 200, "wallet")

    async with database.session() as session:
        history = await repo.recent_transactions(session, GUILD, 5555)

    kinds = {entry.kind for entry in history}
    assert {"admin_give", "admin_take"} <= kinds


async def test_inspect_reports_balances_and_history(admin_cog: Economy, database: Database) -> None:
    await run(admin_cog, "economy_give", StubContext(), target(), 1_500, "bank")
    ctx = StubContext()

    await run(admin_cog, "economy_inspect", ctx, target())

    body = ctx.replies[0]
    assert "1,500" in body
    assert "admin_give" in body


async def test_inspect_explains_an_empty_history(admin_cog: Economy) -> None:
    ctx = StubContext()

    await run(admin_cog, "economy_inspect", ctx, target())

    assert "No logged transactions" in ctx.replies[0]


# -- who may run them --------------------------------------------------------


def permission_context(bot: Bisky, user_id: int, *, administrator: bool) -> Any:
    """A context carrying the guild permissions the checks look at."""
    author = type(
        "M",
        (),
        {
            "id": user_id,
            "display_name": "someone",
            "guild_permissions": discord.Permissions(administrator=administrator),
        },
    )()
    return cast(
        Any,
        type(
            "Ctx",
            (),
            {
                "bot": bot,
                "author": author,
                "guild": StubGuild(GUILD),
                "cog": None,
                "command": None,
                "interaction": None,
            },
        )(),
    )


def command_named(cog: Economy, attribute: str) -> Any:
    return cast(Any, getattr(type(cog), attribute))


async def passes_checks(command: Any, ctx: Any) -> bool:
    """Run only the command's own checks, as Command.can_run would."""
    for predicate in command.checks:
        if not await predicate(ctx):
            return False
    return True


@pytest.mark.parametrize(
    "attribute", ["economy_give", "economy_take", "economy_reset", "economy_inspect"]
)
async def test_admin_commands_reject_a_guild_administrator(
    admin_cog: Economy, admin_bot: Bisky, attribute: str
) -> None:
    """The point of the split: minting aura is not a guild-admin power.

    A server owner tuning their own rates is fine; handing themselves half a
    million aura is not.
    """
    ctx = permission_context(admin_bot, 999, administrator=True)

    with pytest.raises(NotGlobalAdmin):
        await passes_checks(command_named(admin_cog, attribute), ctx)


@pytest.mark.parametrize(
    "attribute", ["economy_give", "economy_take", "economy_reset", "economy_inspect"]
)
async def test_admin_commands_admit_a_global_admin(
    admin_cog: Economy, admin_bot: Bisky, attribute: str
) -> None:
    ctx = permission_context(admin_bot, USER, administrator=False)

    assert await passes_checks(command_named(admin_cog, attribute), ctx) is True


async def test_tuning_commands_still_admit_a_guild_administrator(
    admin_cog: Economy, admin_bot: Bisky
) -> None:
    """`economy set` stays a guild-admin power, unlike `economy give`."""
    ctx = permission_context(admin_bot, 999, administrator=True)

    assert await passes_checks(command_named(admin_cog, "economy_set"), ctx) is True


async def test_tuning_commands_reject_a_plain_member(admin_cog: Economy, admin_bot: Bisky) -> None:
    ctx = permission_context(admin_bot, 999, administrator=False)

    with pytest.raises(NotGuildAdmin):
        await passes_checks(command_named(admin_cog, "economy_set"), ctx)


async def test_every_admin_command_carries_its_own_check(admin_cog: Economy) -> None:
    """A group with invoke_without_command=True does not run its own checks
    when a subcommand is invoked, so an unchecked subcommand is wide open."""
    for attribute in ("economy_give", "economy_take", "economy_reset", "economy_inspect"):
        command = command_named(admin_cog, attribute)
        assert command.checks, f"{attribute} has no check of its own"
