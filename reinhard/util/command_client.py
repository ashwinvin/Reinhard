from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import enum
import importlib
import inspect
import logging
import typing


from hikari.internal_utilities import aio
from hikari.internal_utilities import assertions
from hikari.internal_utilities import containers
from hikari.internal_utilities import loggers
from hikari.internal_utilities import unspecified
from hikari.orm.gateway import event_types as discord_event_types
from hikari.orm.models import bases
from hikari.orm.models import media
from hikari.orm.models import permissions
from hikari.orm import client
from hikari import errors

if typing.TYPE_CHECKING:
    from hikari.internal_utilities import type_hints
    from hikari.orm.http import base_http_adapter
    from hikari.orm.models import embeds
    from hikari.orm.models import guilds
    from hikari.orm.models import messages
    from hikari.orm.state import base_registry
    from hikari.orm import fabric

SEND_MESSAGE_PERMISSIONS = permissions.VIEW_CHANNEL | permissions.SEND_MESSAGES
ATTACH_FILE_PERMISSIONS = SEND_MESSAGE_PERMISSIONS | permissions.ATTACH_FILES
CHARACTERS_TO_SANITIZE = {"@": ""}

# TODO: use command hooks instead of specific stuff like get_guild_prefixes?


class CommandEvents(enum.Enum):
    ERROR = "error"
    LOAD = "load"
    UNLOAD = "unload"

    def __str__(self) -> str:
        return self.value


def sanitize_content(content: str) -> str:
    return content  # TODO: This.


class Executable(abc.ABC):
    @abc.abstractmethod
    async def execute(self, ctx: Context) -> bool:
        ...

    @abc.abstractmethod
    async def check(self, ctx: Context) -> bool:
        ...


class TriggerTypes(enum.Enum):
    PREFIX = enum.auto()
    MENTION = enum.auto()  # TODO: trigger commands with a mention


class PermissionError(errors.HikariError):  # TODO: don't shadow
    __slots__ = ("missing_permissions",)

    missing_permissions: permissions.Permission

    def __init__(
        self, required_permissions: permissions.Permission, actual_permissions: permissions.Permission
    ) -> None:
        pass
        # self.missing_permissions =
        # for permission in m


class Context:
    __slots__ = ("command", "content", "fabric", "message", "trigger", "trigger_type", "triggering_name")

    command: Command

    content: str

    fabric: fabric.Fabric

    #: The message that triggered this command.
    #:
    #: :type: :class:`hikari.orm.models.messages.Message`
    message: messages.Message

    #: The string prefix or mention that triggered this command.
    #:
    #: :type: :class:`str`
    trigger: str

    #: The mention or prefix that triggered this event.
    #:
    #: :type: :class:`TriggerTypes`
    trigger_type: TriggerTypes

    #: The command alias that triggered this command.
    #:
    #: :type: :class:`str`
    triggering_name: str

    def __init__(
        self,
        fabric_obj: fabric.Fabric,
        content: str,
        message: messages.Message,
        trigger: str,
        trigger_type: TriggerTypes,
    ) -> None:
        self.fabric = fabric_obj
        self.content = content
        self.message = message
        self.trigger = trigger
        self.trigger_type = trigger_type

    @property
    def http(self) -> base_http_adapter.BaseHTTPAdapter:
        return self.fabric.http_adapter

    def prune_content(self, length: int):
        self.content = self.content[length:]

    @property
    def state(self) -> base_registry.BaseRegistry:
        return self.fabric.state_registry

    async def reply(
        self,
        *,
        content: type_hints.NotRequired[str] = unspecified.UNSPECIFIED,
        tts: bool = False,
        files: type_hints.NotRequired[typing.Collection[media.AbstractFile]] = unspecified.UNSPECIFIED,
        embed: type_hints.NotRequired[embeds.Embed] = unspecified.UNSPECIFIED,
        soft_send: bool = False,  # TODO: what was this?
        sanitize: bool = False,
    ) -> messages.Message:
        """Used to handle response length and permission checks for command responses."""
        if content is not unspecified.UNSPECIFIED and len(content) > 2000:
            files = files or containers.EMPTY_SEQUENCE
            files.append(media.InMemoryFile("message.txt", bytes(content, "utf-8")))
            content = "This response is too large to send, see attached file."
        elif content is not unspecified.UNSPECIFIED and sanitize:
            content = sanitize_content(content)

        # TODO: this needs to be easier to do on hikari's level.
        # if not files and not SEND_MESSAGE_PERMISSIONS or files and ATTACH_FILE_PERMISSIONS:
        #     raise PermissionError(ATTACH_FILE_PERMISSIONS if files else SEND_MESSAGE_PERMISSIONS)

        return await self.fabric.http_adapter.create_message(
            self.message.channel_id, content=content, tts=tts, embed=embed, files=files
        )

    def set_command_trigger(self, trigger: str):
        self.triggering_name = trigger

    def set_command(self, command_obj: Command):
        self.command = command_obj


@dataclasses.dataclass()
class CommandClientOptions(client.client_options.ClientOptions, bases.MarshalMixin):
    access_levels: typing.MutableMapping[int, int] = dataclasses.field(default_factory=dict)
    # TODO: handle modules (plus maybe other stuff) here?

    def __post_init__(self) -> None:
        self.access_levels = {int(key): value for key, value in self.access_levels.items()}


class CommandError(Exception):
    __slots__ = ("response",)

    #: The string response that the client should send in chat if it has send messages permission.
    #:
    #: :type: :class:`str`
    response: str

    def __init__(self, response: str) -> None:
        self.response = response

    def __str__(self) -> str:
        return self.response


class Command(Executable):
    __slots__ = ("_checks", "_func", "_module", "level", "meta", "triggers")

    _checks: typing.Sequence[CheckLikeT]

    _func: aio.CoroutineFunctionT

    _module: typing.Optional[CommandModule]

    #: The user access level that'll be required to execute this command, defaults to 0.
    #:
    #: :type: :class:`int`
    level: int

    meta: typing.Optional[typing.MutableMapping[typing.Any, typing.Any]]

    #: The triggers used to activate this command in chat along with a prefix.
    #:
    #: :type: :class:`typing.Tuple` of :class:`int`
    triggers: typing.Tuple[str]

    def __init__(
        self,
        func: typing.Optional[aio.CoroutineFunctionT] = None,
        trigger: typing.Optional[str] = None,
        *,
        aliases: typing.Optional[typing.List[str]] = None,
        level: int = 0,
        meta: typing.Optional[typing.MutableMapping[typing.Any, typing.Any]] = None,
        module: typing.Optional[CommandModule] = None,
    ) -> None:
        self._checks = [self.check_prefix_from_context]
        self._func = func
        self.level = level
        self.meta = meta
        self._module = module
        if not trigger:
            trigger = self.generate_trigger()
        self.triggers = tuple(trig for trig in (trigger, *(aliases or containers.EMPTY_COLLECTION)) if trig is not None)

    def __repr__(self) -> str:
        return f"Command({'|'.join(self.triggers)})"

    def bind_module(self, module: CommandModule) -> None:  # TODO: deprecate
        self._module = module

    def deregister_check(self, check: CheckLikeT) -> None:
        try:
            self._checks.remove(check)
        except ValueError:
            raise ValueError("Command Check not found.")

    def register_check(self, check: CheckLikeT) -> None:
        self._checks.append(check)

    async def check(self, ctx: Context) -> bool:
        result: bool = False
        for check in self._checks:
            try:
                if asyncio.iscoroutinefunction(check):
                    result = await check(ctx)
                else:
                    result = check(ctx)
            except Exception:
                result = False
            else:
                if not result:
                    break
        return result

    def check_prefix(self, content: str) -> str:
        for trigger in self.triggers:
            if content.startswith(trigger):
                return trigger

    def check_prefix_from_context(self, ctx: Context) -> bool:
        for trigger in self.triggers:
            if ctx.content.startswith(trigger):
                ctx.set_command_trigger(trigger)
                return True
        return False

    async def execute(self, ctx: Context) -> bool:
        """
        Used to execute a command, catches any :class:`CommandErrors` and calls the module's error handler on error.

        Args:
            message:
                The :class:`hikari.orm.models.messages.Message` object to execute this command using.
            args:
                The string args that followed the triggering prefix and command alias to be parsed.

        Returns:
            An optional :class:`str` response to be sent in chat.
        """
        try:
            await self._func(self._module, ctx, self.parse_args(ctx.content))
        except CommandError as exc:
            with contextlib.suppress(PermissionError):
                await ctx.reply(content=str(exc))
        except Exception as exc:
            await self._module.dispatch_command_event(CommandEvents.ERROR, ctx, exc)  # TODO: move
            raise exc
        return True

    def generate_trigger(self) -> str:
        """Get a trigger for this command based on it's function's name."""
        return self.name.replace("_", " ")

    @property
    def name(self) -> str:
        """Get the name of this command."""
        return self._func.__name__

    def parse_args(self, args: str) -> typing.List[typing.Union[int, str]]:
        return args.split(" ")  # TODO: actually parse


def command(__arg=..., cls=Command, group: typing.Optional[str] = None, **kwargs):  # TODO: handle group...
    # TODO @functools.wraps(coro_fn)
    def decorator(coro_fn):
        return cls(coro_fn, **kwargs)

    return decorator if __arg is ... else decorator(__arg)


class CommandGroup(Executable):
    ...


class CommandModule:
    __slots__ = ("_event_dispatcher", "_fabric", "client", "logger", "module_commands")

    _event_dispatcher: aio.EventDelegate

    _fabric: fabric.Fabric

    #: The command client this module is loaded in.
    #:
    #: :type: :class:`CommandClient` or :class:`None`
    client: typing.Optional[CommandClient]

    #: The class wide logger.
    #:
    #: :type: :class:`logging.Logger`
    logger: logging.Logger

    #: A list of the commands that are loaded in this module.
    #:
    #: :type: :class:`typing.Sequence` of :class:`Command`
    module_commands: typing.List[Command]

    def __init__(self, command_client: typing.Optional[CommandClient] = None, bind: bool = True) -> None:
        super().__init__()
        if command_client:
            self._fabric = command_client._fabric
        self._event_dispatcher = aio.EventDelegate()
        self.logger = loggers.get_named_logger(self)
        if bind:
            self.bind_commands()
            self.bind_listeners()
        self.client = command_client
        self.dispatch_command_event(CommandEvents.LOAD, self)  # TODO: unload and do this somewhere better

    def add_event(
        self, event_name: typing.Union[str, CommandEvents], coroutine_function: aio.CoroutineFunctionT
    ) -> None:
        self.logger.debug(
            "Subscribing %s%s to %s event in %s module.",
            coroutine_function.__name__,
            inspect.signature(coroutine_function),
            event_name,
            self.__class__.__name__,
        )
        self._event_dispatcher.add(str(event_name), coroutine_function)

    def bind_commands(self) -> None:
        """
        Loads any commands that are attached to this class into `module_commands`.

        Raises:
            ValueError:
                if the commands for this module have already been binded or if any duplicate triggers are found while
                loading commands.
        """
        assertions.assert_that(
            not getattr(self, "module_commands", None),
            f"Cannot bind commands in module '{self.__class__.__name__}' when commands have already been binded.",
        )
        self.module_commands = []
        for name, function in inspect.getmembers(self, predicate=lambda func: isinstance(func, Command)):
            function.bind_module(self)
            for trigger in function.triggers:
                if list(self.get_command_from_name(trigger)):
                    self.logger.warning(
                        f"Possible overlapping trigger '%s' found in %s module.", trigger, self.__class__.__name__,
                    )
            self.logger.debug(
                "Binded command %s in %s module.", function.name, self.__class__.__name__,
            )
            self.module_commands.append(function)
        self.module_commands.sort(key=lambda comm: comm.name, reverse=True)

    def bind_listeners(self) -> None:
        for name, function in self.get_module_event_listeners():
            if name not in discord_event_types.EventType.__members__.values():
                self.add_event(name, function)

    def dispatch_command_event(self, event: typing.Union[CommandEvents, str], *args) -> None:
        self.logger.debug("Dispatching %s command event in %s module.", str(event), self.__class__.__name__)
        return self._event_dispatcher.dispatch(str(event), *args)

    async def get_command_from_context(self, ctx: Context) -> typing.AsyncIterator[Command]:
        for command_obj in self.module_commands:
            if await command_obj.check(ctx):
                yield command_obj

    def get_command_from_name(self, content: str) -> typing.Iterator[typing.Tuple[Command, str]]:
        """
        Get a command based on a message's content (minus prefix) from the loaded commands if any command triggers are
        found in the content.

        Args:
            content:
                The string content to try and find a command for (minus the triggering prefix).

        Returns:
            A :class:`typing.Tuple` of :class:`Command` object and the :class:`str` trigger that was matched if the
            command was found else a :class:`typing.Tuple` of :class:`None` and :class:`None`.
        """
        for command_obj in self.module_commands:
            if prefix := command_obj.check_prefix(content):
                yield command_obj, prefix

    def get_module_event_listeners(self) -> typing.Generator[typing.Tuple[str, aio.CoroutineFunctionT]]:
        """Get a generator of the event listeners attached to this module."""
        return (
            (name[3:], function)
            for name, function in inspect.getmembers(self, predicate=asyncio.iscoroutinefunction)
            if name.startswith("on_")
        )

    def register_command(self, func: aio.CoroutineFunctionT, trigger: str = None, *aliases: str) -> None:
        """
        Register a command in this module.

        Args:
            func:
                The Coroutine Function to be called when executing this command.
            trigger:
                The string that will be this command's main trigger.
            *aliases:
                More string triggers for this command.

        Raises:
            ValueError:
                If any of the triggers for this command are found on a loaded command.
        """
        command_obj = Command(func=func, module=self, trigger=trigger, aliases=list(aliases))
        for trigger in command_obj.triggers:
            if list(self.get_command_from_name(trigger)):
                self.logger.warning(
                    f"Possible overlapping trigger '%s' found in %s module.", trigger, self.__class__.__name__,
                )
        self.module_commands.append(command_obj)

    def unregister_command(self, command_obj: typing.Union[Command, str]) -> None:
        if isinstance(command_obj, str):
            try:
                command_obj, prefix = next(self.get_command_from_name(command_obj))
            except StopIteration:
                raise ValueError(f"`{command_obj}` command not found.") from None
        elif not isinstance(command_obj, Command):
            raise ValueError("Command must be string command trigger or a 'Command' object.") from None

        try:
            self.module_commands.remove(command_obj)
        except ValueError:
            raise ValueError("Invalid command passed for this module.") from None


class CommandClient(CommandModule, client.Client):
    """
    The central client that all command modules will be binded to. This extends :class:`hikari.client.Client` and
    handles registering event listeners attached to the loaded modules and the listener(s) required for commands.

    Note:
        This inherits from :class:`CommandModule` and can act as an independent Command Module for small bots.
    """

    __slots__ = ("get_guild_prefix", "modules", "prefixes")

    get_guild_prefix: typing.Union[aio.CoroutineFunctionT, None]  # TODO: or normal method.
    # TODO: rename this to something singular

    #: The command modules that are loaded in this client.
    #:
    #: :type: :class:`typing.MutableMapping` of :class:`str` to :class:`CommandModule`
    modules: typing.MutableMapping[str, CommandModule]

    #: An array of this bot's global prefixes.
    #:
    #: :type: :class:`typing.List` of :class:`str`
    prefixes: typing.List[str]

    def __init__(
        self,
        prefixes: typing.List[str],
        *,
        modules: typing.List[str] = None,
        options: typing.Optional[CommandClientOptions] = None,
    ) -> None:
        super().__init__(bind=False)
        self.modules = {}
        self.load_modules(*(modules or containers.EMPTY_SEQUENCE))
        self.bind_commands()
        self.bind_listeners()
        self.prefixes = prefixes
        if options:
            self._client_options = options
        # TODO: built in help command.

    def add_event(self, event_name: str, coroutine_function: aio.CoroutineFunctionT) -> None:
        if event_name in discord_event_types.EventType.__members__.values():
            client.Client.add_event(self, event_name, coroutine_function)
        else:
            super().add_event(event_name, coroutine_function)

    async def access_check(self, command_obj: Command, message: messages.Message) -> bool:
        """
        Used to check if a command can be accessed by the calling user and in the calling channel/guild.

        Args:
            command_obj:
                The :class:`Command` object to check access levels for.
            message:
                The :class:`messages.Message` object to check access levels for.

        Returns:
            A :class:`bool` representation of whether this command can be accessed.
        """
        return self._client_options.access_levels.get(message.author.id, 0) >= command_obj.level  # TODO: sql filter.

    def bind_listeners(self) -> None:
        """Used to add event listeners from all loaded command modules to hikari's internal event listener."""
        for module in (self, *self.modules.values()):
            for name, function in module.get_module_event_listeners():
                if name in discord_event_types.EventType.__members__.values():
                    self.add_event(name, function)
        super().bind_listeners()

    async def check_prefix(self, message: messages.Message) -> typing.Optional[str]:
        """
        Used to check if a message's content match any currently registered prefix (including any prefixes registered
        for the guild if this is being called from one.

        Args:
            message:
                The :class:`messages.Message` object that we're checking for a prefix in it's content.

        Returns:
            A :class:`str` representation of the triggering prefix if found, else :class:`None`
        """
        trigger_prefix = None
        for prefix in await self._get_prefixes(message.guild_id):
            if message.content.startswith(prefix):
                trigger_prefix = prefix
                break
        return trigger_prefix

    async def get_global_command_from_context(self, ctx: Context) -> typing.AsyncIterator[Command]:
        """
        Used to get a command from on a messages's content (checks all loaded modules).

        Args:
            content:
                The :class:`str` content of the message (minus the prefix) to get a command from.

        Returns:
            A :class:`typing.Tuple` of the :class:`Command` object and the :class:`str` trigger that was matched if
            the command was found found, else a :class:`typing.Tuple` of :class:`None` and :class:`None`.
        """
        for module in (self, *self.modules.values()):
            async for command_obj in module.get_command_from_context(ctx):
                yield command_obj

    def get_global_command_from_name(self, content: str) -> typing.Iterator[typing.Tuple[Command, str]]:
        yield from self.get_command_from_name(content)
        for module in self.modules.values():
            yield from module.get_command_from_name(content)

    async def _get_prefixes(self, guild: typing.Optional[guilds.GuildLikeT]) -> typing.List[str]:
        """
        Used to get the registered global prefixes and a guild's prefix from the function `get_guild_prefix` if this is
        being called from a guild and `get_guild_prefix` has been implemented on this object.

        Args:
            guild:
                The object or ID of the guild to check or :class:`None`.

        Returns:
            An :class:`typing.Sequence` of :class:`str` representation of the applicable prefixes.
        """
        if guild is None or not hasattr(self, "get_guild_prefix"):
            return self.prefixes

        if asyncio.iscoroutinefunction(self.get_guild_prefix):
            guild_prefix = await self.get_guild_prefix(int(guild))  # TODO: maybe don't
        else:
            guild_prefix = self.get_guild_prefix(int(guild))

        return [guild_prefix, *self.prefixes] if guild_prefix else self.prefixes

    def load_modules(self, *modules: str) -> None:
        """
        Used to load modules based on string paths.

        Args:
            *modules:
                The :class:`str` paths of modules to load (in the format of `root.dir.module`)
        """
        for module_path in modules:
            found = False
            module = importlib.import_module(module_path)
            for attr in dir(module):
                value = getattr(module, attr)
                if inspect.isclass(value) and issubclass(value, CommandModule) and value is not CommandModule:
                    self.modules[value.__class__.__name__] = value(self)  # TODO: setup or something smarter
                    found = True
            if not found:
                raise ValueError(f"No valid 'CommandModule' derived class found in '{module_path}'.")

    async def on_message_create(self, message: messages.Message) -> None:
        """Handles command triggering based on message creation."""
        prefix = await self.check_prefix(message)  # TODO: maybe one day we won't have to await this.
        mention = None  # TODO: mention at end of message?
        if prefix or mention:
            command_args = message.content[len(prefix or mention) :]
        else:
            return

        ctx = Context(
            self._fabric,
            command_args,
            message,
            prefix or mention,
            TriggerTypes.PREFIX if prefix else TriggerTypes.MENTION,
        )
        async for command_obj in self.get_global_command_from_context(ctx):
            if await self.access_check(command_obj, message):
                ctx.set_command(command_obj)
                break
            else:
                command_obj = None
        else:
            command_obj = None

        if command_obj is None:
            return

        ctx.prune_content(len(ctx.triggering_name) + 1)

        await command_obj.execute(ctx)


CheckLikeT = typing.Callable[[Context], typing.Union[bool, typing.Coroutine[typing.Any, typing.Any, bool]]]


__all__ = [
    "command",
    "Command",
    "CommandClient",
    "CommandClientOptions",
    "CommandError",
    "CommandModule",
]
