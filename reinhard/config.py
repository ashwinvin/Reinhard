from __future__ import annotations

import attr
import typing

from hikari import bases
from hikari.internal import marshaller
from tanjun import configs


@marshaller.marshallable()
@attr.s(slots=True, kw_only=True)
class DatabaseConfig(marshaller.Deserializable):
    password: str = marshaller.attrib(repr=False, deserializer=str)
    database: str = marshaller.attrib(repr=False, deserializer=str, if_undefined=lambda: "postgres", default="postgres")
    host: str = marshaller.attrib(repr=False, deserializer=str, if_undefined=lambda: "localhost", default="localhost")
    port: int = marshaller.attrib(repr=False, deserializer=int, if_undefined=lambda: 5432, factory=lambda: 5432)
    user: str = marshaller.attrib(repr=False, deserializer=str, if_undefined=lambda: "postgres", default="postgres")


@marshaller.marshallable()
@attr.s(slots=True, kw_only=True)
class ExtendedOptions(configs.ClientConfig):
    database: DatabaseConfig = marshaller.attrib(deserializer=DatabaseConfig.deserialize, factory=DatabaseConfig)
    emoji_guild: typing.Optional[bases.Snowflake] = marshaller.attrib(
        deserializer=bases.Snowflake, if_undefined=None, default=None
    )
    file_log_level: str = marshaller.attrib(deserializer=str, if_undefined=lambda: "WARNING", default="WARNING")
    log_level: str = marshaller.attrib(deserializer=str, if_undefined=lambda: "INFO", default="INFO")
    prefixes: typing.List[str] = marshaller.attrib(
        deserializer=lambda payload: [str(prefix) for prefix in payload],
        if_undefined=lambda: ["."],
        factory=lambda: ["."],
    )
