"""Data access helpers, kept out of cog logic so they can be tested directly.

Re-exported here so callers import from ``bisky.db.repository`` regardless of
which submodule a helper lives in.
"""

from bisky.db.repository.admins import (
    count_global_admins,
    grant_global_admin,
    is_global_admin,
    list_global_admins,
    revoke_global_admin,
    seed_global_admins,
)
from bisky.db.repository.cogs import (
    disable_cog,
    enable_cog,
    enabled_cogs,
    guilds_with_cog,
)
from bisky.db.repository.invocations import count_invocations, record_invocation
from bisky.db.repository.settings import (
    get_guild_prefix,
    get_guild_settings,
    get_user_settings,
    set_guild_prefix,
    set_user_settings,
)

__all__ = [
    "count_global_admins",
    "count_invocations",
    "disable_cog",
    "enable_cog",
    "enabled_cogs",
    "get_guild_prefix",
    "get_guild_settings",
    "get_user_settings",
    "grant_global_admin",
    "guilds_with_cog",
    "is_global_admin",
    "list_global_admins",
    "record_invocation",
    "revoke_global_admin",
    "seed_global_admins",
    "set_guild_prefix",
    "set_user_settings",
]
