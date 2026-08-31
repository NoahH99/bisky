"""The help command.

``DefaultHelpCommand`` renders a wall of monospaced text and only reveals a
group's subcommands via ``!help <group>``, which is not what anyone types. This
module replaces it with embeds and makes ``<group> help`` work as well.

Filtering is inherited from :class:`~discord.ext.commands.HelpCommand`, which
runs each command's checks before listing it. That matters here: the per-guild
cog gate is a global check, so commands belonging to a cog that is disabled in
this guild are omitted rather than advertised and then refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from discord.ext import commands

EMBED_COLOUR = discord.Colour.blurple()

#: Discord rejects embeds whose field values exceed this.
MAX_FIELD_LENGTH = 1024


def summarise(command: commands.Command[Any, ..., Any]) -> str:
    """The one-line description shown in listings."""
    return command.short_doc or "No description."


def truncate(text: str, limit: int = MAX_FIELD_LENGTH) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BiskyHelpCommand(commands.HelpCommand):
    """Embed-based help."""

    def __init__(self) -> None:
        super().__init__(
            command_attrs={
                "help": "Show this message, or details for a command.",
                "aliases": ["commands"],
            }
        )

    @property
    def prefix(self) -> str:
        return self.context.clean_prefix

    def embed(self, title: str, description: str | None = None) -> discord.Embed:
        return discord.Embed(title=title, description=description, colour=EMBED_COLOUR)

    def signature_of(self, command: commands.Command[Any, ..., Any]) -> str:
        return f"{self.prefix}{command.qualified_name} {command.signature}".strip()

    async def visible(
        self, candidates: list[commands.Command[Any, ..., Any]]
    ) -> list[commands.Command[Any, ..., Any]]:
        """Commands the invoker can actually run here, sorted by name."""
        return list(await self.filter_commands(candidates, sort=True))

    def describe_all(self, entries: list[commands.Command[Any, ..., Any]]) -> str:
        return "\n".join(f"`{command.name}` — {summarise(command)}" for command in entries)

    # -- entry points ---------------------------------------------------------

    async def send_bot_help(
        self,
        mapping: Mapping[commands.Cog | None, list[commands.Command[Any, ..., Any]]],
    ) -> None:
        embed = self.embed(
            "Bisky",
            f"`{self.prefix}help <command>` for details on any command.\n"
            f"Most commands also work as `/` slash commands.",
        )

        for cog, cog_commands in sorted(
            mapping.items(), key=lambda item: item[0].qualified_name if item[0] else "zzz"
        ):
            entries = await self.visible(cog_commands)
            if not entries:
                continue
            name = cog.qualified_name if cog else "Other"
            embed.add_field(name=name, value=truncate(self.describe_all(entries)), inline=False)

        if not embed.fields:
            embed.description = (
                "No commands are available to you here. An admin may need to "
                f"enable a cog with `{self.prefix}cogs enable <name>`."
            )
        await self.get_destination().send(embed=embed)

    async def send_cog_help(self, cog: commands.Cog) -> None:
        entries = await self.visible(list(cog.get_commands()))
        embed = self.embed(cog.qualified_name, cog.description or None)
        if entries:
            embed.add_field(name="Commands", value=truncate(self.describe_all(entries)))
        else:
            embed.description = "Nothing here is available to you."
        await self.get_destination().send(embed=embed)

    async def send_group_help(self, group: commands.Group[Any, ..., Any]) -> None:
        embed = self.embed(
            f"{self.prefix}{group.qualified_name}", group.help or group.short_doc or None
        )
        embed.add_field(name="Usage", value=f"`{self.signature_of(group)}`", inline=False)

        entries = await self.visible(list(group.commands))
        if entries:
            lines = [f"`{self.signature_of(sub)}`\n{summarise(sub)}" for sub in entries]
            embed.add_field(name="Subcommands", value=truncate("\n".join(lines)), inline=False)
        self.add_aliases(embed, group)
        await self.get_destination().send(embed=embed)

    async def send_command_help(self, command: commands.Command[Any, ..., Any]) -> None:
        embed = self.embed(
            f"{self.prefix}{command.qualified_name}", command.help or command.short_doc or None
        )
        embed.add_field(name="Usage", value=f"`{self.signature_of(command)}`", inline=False)
        self.add_aliases(embed, command)
        await self.get_destination().send(embed=embed)

    def add_aliases(self, embed: discord.Embed, command: commands.Command[Any, ..., Any]) -> None:
        if command.aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`{alias}`" for alias in command.aliases),
                inline=False,
            )

    async def send_error_message(self, error: str) -> None:
        await self.get_destination().send(embed=self.embed("Not found", error))


def _help_callback(group: commands.Group[Any, ..., Any], *, bound_to_cog: bool) -> Any:
    """Build a callback bound to one group.

    A factory rather than a default argument: a default would bind correctly in
    the loop but then show up in the command's signature as ``[_group]``.

    The arity has to match how discord.py invokes commands — ``ctx.args = [ctx]
    if self.cog is None else [self.cog, ctx]`` (``ext/commands/core.py``). Since
    these helpers inherit the parent's cog so its checks still apply, they are
    almost always called with the cog first, and a one-argument callback raises
    ``TypeError`` at invoke time.
    """
    if bound_to_cog:

        async def show_help_in_cog(_cog: Any, ctx: commands.Context[Any]) -> None:
            await ctx.send_help(group)

        return show_help_in_cog

    async def show_help(ctx: commands.Context[Any]) -> None:
        await ctx.send_help(group)

    return show_help


def attach_group_help(bot: commands.Bot) -> int:
    """Give every command group a ``help`` subcommand.

    ``!economy help`` is what people type, but discord.py only understands
    ``!help economy``: with ``invoke_without_command=True`` the unmatched word
    is passed to the group callback and silently ignored. Registering a real
    ``help`` subcommand on each group closes that gap once, rather than every
    group having to remember to handle it.

    Returns how many were attached, so startup can log it.
    """
    attached = 0
    for command in list(bot.walk_commands()):
        if not isinstance(command, commands.Group) or "help" in command.all_commands:
            continue

        helper: commands.Command[Any, ..., None] = commands.Command(
            _help_callback(command, bound_to_cog=command.cog is not None),
            name="help",
            help=f"Show help for {command.qualified_name}.",
            hidden=True,
        )
        # Inherit the parent's cog so cog_check still applies; otherwise
        # `!admin help` would bypass the owner gate the rest of the group has.
        helper.cog = command.cog
        # discord.py decides how many leading parameters to hide from
        # get_signature_parameters via is_inside_class(), which is False for a
        # closure, so it only ever skips one. With the (cog, ctx) arity that
        # leaves ctx showing up as a user argument in the usage string. The
        # command takes no user input at all, so say so.
        helper.params = {}
        command.add_command(helper)
        attached += 1
    return attached
