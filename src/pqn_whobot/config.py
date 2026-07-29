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
from pydantic import model_validator
from pydantic_settings import BaseSettings
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from pydantic_settings import TomlConfigSettingsSource

REBOOT_TIMEOUT_S = 360.0
"""Outer bound on a whole Reboot invocation, as ``@action`` declares it.

It lives here rather than beside the Action because it is the ceiling ``reboot_wait_s`` is
checked against, and this module may not import ``whobot.py`` — the dependency runs the other
way. An Action's ``timeout_s`` is read by the scan while the class is being created, before
any settings exist, so it cannot itself come from configuration.
"""


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

    # Bound for one Node API call made by an Action. Neither existing key fits: 5s is for
    # "are you there?", and 900s is the digest's whole budget for a Node.
    node_timeout_s: float = Field(default=30.0, gt=0)

    # How long a rebooted Node has to answer again before it is reported as still down.
    # Site-specific, which is why it is here: a machine with a slow POST, or one that fscks
    # on boot, legitimately takes longer than one that does not.
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

    @model_validator(mode="after")
    def _reboot_must_be_able_to_report_itself(self) -> "WhobotSettings":
        """Keep the reboot wait inside what the Reboot Action is allowed to take.

        A wait that outlasts the Action's ``timeout_s`` is worse than a shorter one:
        ``execute`` cuts the run off and posts "Timed out after 360s", losing the "still down
        after 5 minutes — go and look at the machine" report the wait exists to produce.

        The three keys are added because they are what one invocation spends: the call that
        asks for the reboot, the wait, and the last poll of that wait.
        """
        budget = self.node_timeout_s + self.reboot_wait_s + self.reachability_timeout_s
        if budget > REBOOT_TIMEOUT_S:
            msg = (
                f"reboot_wait_s ({self.reboot_wait_s:.0f}s) leaves the Reboot Action no time to report: "
                f"with node_timeout_s ({self.node_timeout_s:.0f}s) and reachability_timeout_s "
                f"({self.reachability_timeout_s:.0f}s) it needs {budget:.0f}s of the {REBOOT_TIMEOUT_S:.0f}s "
                f"the Action is allowed. Lower reboot_wait_s to at most "
                f"{REBOOT_TIMEOUT_S - self.node_timeout_s - self.reachability_timeout_s:.0f}s."
            )
            raise ValueError(msg)
        return self

    @property
    def timezone(self) -> ZoneInfo:
        """The zone ``schedule_hour`` and ``schedule_minute`` are interpreted in."""
        return ZoneInfo(self.schedule_timezone)


def config_path() -> Path:
    """Return the file settings are loaded from, relative to the working directory."""
    # pydantic-settings types this as "one path, or a list of them, or None"; ours is one path.
    return Path(WhobotSettings.model_config["toml_file"])  # type: ignore[arg-type]
