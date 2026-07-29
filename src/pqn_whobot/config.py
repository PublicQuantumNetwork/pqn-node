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

    # How the digest measures a Node. The address is resolved *on the Node*, so 127.0.0.1
    # means each Node's own host and one value serves the fleet; the port is the Node API's.
    timetagger_address: str = "127.0.0.1:9000"
    basis: tuple[float, float] = (0.0, 22.5)

    # How long one Game may take. The digest's per-Node and whole-run bounds are derived from
    # this rather than configured beside it, so no two keys can disagree about the same wait.
    per_game_timeout_s: float = Field(default=600.0, gt=0)

    # Bound for a single "are you there?" call, so listing Nodes cannot stall on a dead one.
    reachability_timeout_s: float = Field(default=5.0, gt=0)

    # Bound for one Node API call that does work — reading a config, writing availability,
    # capturing a screenshot. Longer than "are you there?", far shorter than a Game.
    node_timeout_s: float = Field(default=30.0, gt=0)

    # How long a rebooted Node has to answer again before it is reported as still down.
    # Site-specific, which is why it is here: a machine with a slow POST, or one that fscks
    # on boot, legitimately takes longer than one that does not. The Reboot Action's own bound
    # is worked out from this, so raising it cannot cut the report it exists to produce short.
    reboot_wait_s: float = Field(default=300.0, gt=0)

    # Development only: answer the Screenshot Action with this file instead of calling a
    # Node. Screenshot is the one Action that needs a Node in front of a real display —
    # X11, KDE and `maim` — which a laptop has none of, so without this the Action cannot
    # be exercised at all until Phase 8. Set it and Whobot never calls the Node; the report
    # says so rather than passing the file off as the Node's display.
    debug_screenshot_path: Path | None = None

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
