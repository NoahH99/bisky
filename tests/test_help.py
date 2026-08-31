"""Tests for the help command.

Two behaviours matter beyond formatting: `<group> help` must work, because that
is what people type, and the help listing must not advertise commands the
invoker cannot run.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

import discord
import pytest
from discord.ext import commands

from bisky.bot import Bisky
from bisky.config import Settings
from bisky.db.session import Database
from bisky.help import BiskyHelpCommand, attach_group_help, summarise, truncate


class Destination:
    def __init__(self) -> None:
        self.embeds: list[discord.Embed] = []

    async def send(self, *, embed: discord.Embed | None = None, **_: Any) -> None:
        assert embed is not None
        self.embeds.append(embed)


@pytest.fixture
async def bot(settings: Settings, database: Database) -> Bisky:
    bot = Bisky(settings, database)
    await bot.load_extensions()
    attach_group_help(bot)
    return bot


@pytest.fixture
def rendered(bot: Bisky) -> tuple[BiskyHelpCommand, Destination]:
    """A help command wired to a capture destination, with filtering stubbed.

    filter_commands needs a real Context to run checks; these tests cover
    formatting and structure, and the filtering behaviour is asserted
    separately via the hidden flag.
    """
    help_command = cast(BiskyHelpCommand, bot.help_command)
    destination = Destination()
    help_command.context = cast(Any, type("Ctx", (), {"clean_prefix": "!", "bot": bot})())
    help_command.get_destination = lambda: cast(Any, destination)  # type: ignore[method-assign]

    async def passthrough(
        cmds: Any, *, sort: bool = False, key: Any = None
    ) -> list[commands.Command[Any, ..., Any]]:
        return sorted(cmds, key=lambda c: c.name)

    help_command.filter_commands = passthrough  # type: ignore[method-assign]
    return help_command, destination


def field(embed: discord.Embed, name: str) -> str:
    for item in embed.fields:
        if item.name == name:
            return str(item.value)
    raise AssertionError(f"no {name!r} field in {[f.name for f in embed.fields]}")


def test_default_help_command_is_replaced(bot: Bisky) -> None:
    assert isinstance(bot.help_command, BiskyHelpCommand)


# -- <group> help ------------------------------------------------------------


def test_every_group_gains_a_help_subcommand(bot: Bisky) -> None:
    """`!economy help` is what people type; discord.py only knows `!help economy`."""
    groups = [c for c in bot.walk_commands() if isinstance(c, commands.Group)]

    assert groups
    for group in groups:
        assert "help" in group.all_commands, f"{group.qualified_name} has no help subcommand"


def test_group_help_takes_no_arguments(bot: Bisky) -> None:
    """Binding the group via a default argument would leak it into the usage."""
    helper = cast(commands.Group[Any, ..., Any], bot.get_command("economy")).all_commands["help"]

    assert helper.signature == ""


def test_group_help_is_hidden_from_listings(bot: Bisky) -> None:
    helper = cast(commands.Group[Any, ..., Any], bot.get_command("economy")).all_commands["help"]

    assert helper.hidden is True


def test_group_help_inherits_the_parent_cog(bot: Bisky) -> None:
    """Otherwise `!admin help` would bypass the cog check the group relies on."""
    admin = cast(commands.Group[Any, ..., Any], bot.get_command("admin"))
    helper = admin.all_commands["help"]

    assert helper.cog is admin.cog
    assert helper.cog is not None


def test_attaching_twice_does_not_duplicate(bot: Bisky) -> None:
    added = attach_group_help(bot)

    assert added == 0


async def invoke_like_discord(command: commands.Command[Any, ..., Any], ctx: Any) -> None:
    """Call a command's callback exactly as discord.py would.

    ``Command.invoke`` builds ``ctx.args = [ctx] if self.cog is None else
    [self.cog, ctx]`` (``ext/commands/core.py``). Calling the callback directly
    with just the context bypasses that and hides arity bugs — which is how a
    one-argument callback on a cog-bound command shipped and then raised
    TypeError on the first real invocation.
    """
    args = [ctx] if command.cog is None else [command.cog, ctx]
    await cast(Any, command.callback)(*args)


@pytest.mark.parametrize("group_name", ["economy", "lottery", "prefix", "cogs", "admin"])
async def test_group_help_subcommand_is_callable_as_discord_calls_it(
    bot: Bisky, group_name: str
) -> None:
    group = cast(commands.Group[Any, ..., Any], bot.get_command(group_name))
    sent: list[Any] = []

    class Ctx:
        async def send_help(self, target: Any) -> None:
            sent.append(target)

    await invoke_like_discord(group.all_commands["help"], Ctx())

    assert sent == [group]


def test_help_callback_arity_matches_the_cog_convention(bot: Bisky) -> None:
    """A cog-bound command is called as (cog, ctx); a cog-less one as (ctx)."""
    for command in bot.walk_commands():
        if not isinstance(command, commands.Group):
            continue
        helper = command.all_commands["help"]
        expected = 2 if helper.cog is not None else 1
        actual = len(inspect.signature(cast(Any, helper.callback)).parameters)
        assert actual == expected, f"{command.qualified_name} help takes {actual} args"


# -- rendering ---------------------------------------------------------------


async def test_group_help_lists_subcommands_with_usage(
    bot: Bisky, rendered: tuple[BiskyHelpCommand, Destination]
) -> None:
    help_command, destination = rendered

    await help_command.send_group_help(cast(Any, bot.get_command("economy")))

    embed = destination.embeds[-1]
    subcommands = field(embed, "Subcommands")
    assert "!economy set <field> <value>" in subcommands
    assert "!economy role" in subcommands
    assert field(embed, "Usage") == "`!economy`"
    assert field(embed, "Aliases") == "`eco`"


async def test_command_help_shows_usage_and_aliases(
    bot: Bisky, rendered: tuple[BiskyHelpCommand, Destination]
) -> None:
    help_command, destination = rendered

    await help_command.send_command_help(cast(Any, bot.get_command("coinflip")))

    embed = destination.embeds[-1]
    assert field(embed, "Usage") == "`!coinflip <amount> [call=heads]`"
    assert "`cf`" in field(embed, "Aliases")


async def test_bot_help_groups_by_cog(
    bot: Bisky, rendered: tuple[BiskyHelpCommand, Destination]
) -> None:
    help_command, destination = rendered
    mapping: dict[Any, list[Any]] = {cog: list(cog.get_commands()) for cog in bot.cogs.values()}

    await help_command.send_bot_help(mapping)

    embed = destination.embeds[-1]
    names = {f.name for f in embed.fields}
    assert {"Economy", "Gambling", "Ping"} <= names
    assert "balance" in field(embed, "Economy")


async def test_bot_help_says_so_when_nothing_is_available(
    bot: Bisky, rendered: tuple[BiskyHelpCommand, Destination]
) -> None:
    """A guild with every feature cog disabled should get an explanation."""
    help_command, destination = rendered

    await help_command.send_bot_help({})

    assert "enable a cog" in str(destination.embeds[-1].description)


async def test_cog_help_lists_its_commands(
    bot: Bisky, rendered: tuple[BiskyHelpCommand, Destination]
) -> None:
    help_command, destination = rendered

    await help_command.send_cog_help(cast(Any, bot.get_cog("Ping")))

    assert "ping" in field(destination.embeds[-1], "Commands")


async def test_unknown_command_gets_an_error_embed(
    bot: Bisky, rendered: tuple[BiskyHelpCommand, Destination]
) -> None:
    help_command, destination = rendered

    await help_command.send_error_message("No command called 'nope' found.")

    assert destination.embeds[-1].title == "Not found"


# -- helpers -----------------------------------------------------------------


def test_summarise_falls_back_when_undocumented() -> None:
    command = cast(Any, type("C", (), {"short_doc": ""})())

    assert summarise(command) == "No description."


def test_truncate_respects_the_embed_field_limit() -> None:
    assert truncate("x" * 2000).endswith("…")
    assert len(truncate("x" * 2000)) == 1024
    assert truncate("short") == "short"
