from __future__ import annotations

__all__: typing.Sequence[str] = [
    "RESTFulMemberConverter",
    "RESTFulRoleConverter",
    "RESTFulUserConverter",
    # tanjun.conversion
    "ChannelConverter",
    "ColorConverter",
    "EmojiConverter",
    "GuildConverter",
    "InviteConverter",
    "MemberConverter",
    "PresenceConverter",
    "RoleConverter",
    "SnowflakeConverter",
    "UserConverter",
    "VoiceStateConverter",
]

import typing

from hikari import errors as hikari_errors
from tanjun import conversion
from tanjun.conversion import *
from yuyo import backoff

from reinhard.util import basic
from reinhard.util import rest_manager

if typing.TYPE_CHECKING:
    from hikari import guilds
    from hikari import snowflakes
    from hikari import users
    from tanjun import traits


class RESTFulMemberConverter(MemberConverter):
    __slots__: typing.Sequence[str] = ()

    @classmethod
    def cache_bound(cls) -> bool:
        return False

    @classmethod
    async def convert(cls, ctx: traits.Context, argument: str, /) -> guilds.Member:
        if ctx.message.guild_id is None:
            raise ValueError("Cannot get a member from a DM channel")

        try:
            # Always try the cache first.
            return await super().convert(ctx, argument)
        except ValueError:
            pass

        member_id: typing.Optional[snowflakes.Snowflake] = None
        try:
            member_id = conversion.UserIDParser.match_id(argument)
        except ValueError:
            pass

        retry = backoff.Backoff(max_retries=5)
        error_manager = (
            rest_manager.HikariErrorManager(retry).with_rule(
                # We catch a IndexError for search_members where a dynamic length list is returned.
                (hikari_errors.BadRequestError, hikari_errors.NotFoundError, IndexError),
                basic.raise_error("Couldn't find member.", error_type=ValueError),
            )
            # If this is the case then we can't access the guild this was triggered in anymore and should stop the
            # command from trying to execute without replying.
            .with_rule((hikari_errors.ForbiddenError,), basic.raise_error(None))
        )

        async for _ in retry:
            with error_manager:
                # Get by ID if we were provided a valid ID.
                if member_id is not None:
                    return await ctx.client.rest_service.rest.fetch_member(ctx.message.guild_id, member_id)

                # Else get by username/nickname.
                else:
                    return (await ctx.client.rest_service.rest.search_members(ctx.message.guild_id, argument))[0]

        else:
            raise ValueError("Couldn't get member in time") from None


class RESTFulRoleConverter(RoleConverter):
    __slots__: typing.Sequence[str] = ()

    @classmethod
    def cache_bound(cls) -> bool:
        return False

    @classmethod
    async def convert(cls, ctx: traits.Context, argument: str, /) -> guilds.Role:
        # This is more strict than RoleConverter but having it consistently reject DM channels is preferable over it
        # rejecting DM channels once it fails to find anything in the cache.
        if ctx.message.guild_id is None:
            raise ValueError("Cannot get a role from a DM channel")

        try:
            # Always try the cache first.
            return await super().convert(ctx, argument)
        except ValueError:
            pass

        # Match by ID if we were provided a valid ID.
        try:
            role_id = conversion.UserIDParser.match_id(argument)

            def predicate(role: guilds.Role) -> bool:
                return role.id == role_id

        # Else match by name.
        except ValueError:
            argument = argument.casefold()

            def predicate(role: guilds.Role) -> bool:
                return role.name.casefold() == argument

        retry = backoff.Backoff(max_retries=5)
        error_manager = (
            rest_manager.HikariErrorManager(retry).with_rule(
                # next(...) will raise StopIteration if the iterator feed to it doesn't yield anything.
                (hikari_errors.BadRequestError, hikari_errors.NotFoundError, StopIteration),
                basic.raise_error("Couldn't find role.", error_type=ValueError),
            )
            # If this is the case then we can't access the guild this was triggered in anymore and should stop the
            # command from trying to execute without replying.
            .with_rule((hikari_errors.ForbiddenError,), basic.raise_error(None))
        )

        async for _ in retry:
            with error_manager:
                roles = await ctx.client.rest_service.rest.fetch_roles(ctx.message.guild_id)
                return next(filter(predicate, iter(roles)))

        else:
            raise ValueError("Couldn't fetch user in time.")


class RESTFulUserConverter(UserConverter):
    __slots__: typing.Sequence[str] = ()

    @classmethod
    def cache_bound(cls) -> bool:
        return False

    @classmethod
    async def convert(cls, ctx: traits.Context, argument: str, /) -> users.User:
        try:
            # Always try the cache first.
            return await super().convert(ctx, argument)
        except ValueError:
            pass

        user_id = conversion.UserIDParser.match_id(argument, message="No valid user mention or ID found")

        retry = backoff.Backoff(max_retries=5)
        error_manager = rest_manager.HikariErrorManager(retry).with_rule(
            (hikari_errors.BadRequestError, hikari_errors.NotFoundError),
            basic.raise_error("Couldn't find user.", error_type=ValueError),
        )

        async for _ in retry:
            with error_manager:
                return await ctx.client.rest_service.rest.fetch_user(user_id)

        else:
            raise ValueError("Couldn't fetch user in time.")
