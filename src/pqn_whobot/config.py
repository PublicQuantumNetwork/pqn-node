"""Whobot's configuration, read from ``whobot.toml`` in the working directory.

``WhobotSettings()`` loads it. A missing file loads as all-defaults — no tokens, no Nodes —
so callers that need a real config check that the file exists first.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from pydantic_settings import TomlConfigSettingsSource


class NodeEntry(BaseModel):
    """One entry of the Node Registry: a Node's address.

    Names are not configured here; Whobot reads each Node's name from the Node itself.
    """

    model_config = ConfigDict(extra="forbid")

    api_url: str

    @field_validator("api_url")
    @classmethod
    def _require_absolute_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            msg = f"api_url must start with http:// or https:// (got {value!r})"
            raise ValueError(msg)
        return value.rstrip("/")


class WhobotSettings(BaseSettings):
    """Everything Whobot needs to run. Unknown keys in the file are rejected.

    The Slack tokens default to empty, so Node-facing commands run without credentials.
    """

    slack_bot_token: str = ""
    slack_app_token: str = ""
    digest_channel: str = ""

    # Daily Digest schedule, interpreted in schedule_timezone rather than the host's zone.
    schedule_timezone: str = "America/Chicago"
    schedule_hour: int = Field(default=7, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)

    # Bounds for the serial digest, per Node rather than one bound for the whole run.
    per_node_timeout_s: float = Field(default=900.0, gt=0)
    per_game_timeout_s: float = Field(default=600.0, gt=0)

    # Bound for a single "are you there?" call, well under the digest's per-Node budget.
    reachability_timeout_s: float = Field(default=5.0, gt=0)

    # What a digest run records about itself.
    last_run_at: datetime | None = None
    last_result: str | None = None

    # The Node Registry: the Nodes Whobot knows about.
    nodes: list[NodeEntry] = Field(default_factory=list)

    model_config = SettingsConfigDict(
        toml_file="./whobot.toml",
        extra="forbid",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        # Unused, but pydantic-settings passes them by keyword, so the names must stay.
        env_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Take values from ``whobot.toml``, then from keyword arguments."""
        return (
            TomlConfigSettingsSource(settings_cls),
            init_settings,
        )

    @field_validator("schedule_timezone")
    @classmethod
    def _require_known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as e:
            msg = f"schedule_timezone {value!r} is not a known IANA timezone: {e}"
            raise ValueError(msg) from e
        return value

    @property
    def timezone(self) -> ZoneInfo:
        """The zone ``schedule_hour`` and ``schedule_minute`` are interpreted in."""
        return ZoneInfo(self.schedule_timezone)


def config_path() -> Path:
    """Return the file settings are loaded from, relative to the working directory."""
    # pydantic-settings types this as "one path, or a list of them, or None"; ours is one path.
    return Path(WhobotSettings.model_config["toml_file"])  # type: ignore[arg-type]
