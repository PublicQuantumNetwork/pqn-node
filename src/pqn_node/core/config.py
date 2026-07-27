import asyncio
import logging
import os
import tempfile
from collections.abc import Mapping
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import tomlkit
from pqn_hardware.measurement import MeasurementConfig
from pydantic import BaseModel
from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import PydanticBaseSettingsSource
from pydantic_settings import SettingsConfigDict
from pydantic_settings import TomlConfigSettingsSource

from pqn_node.constants import BellState
from pqn_node.constants import QKDEncodingBasis

logger = logging.getLogger(__name__)


class DailyReportConfig(BaseModel):
    slack_webhook_url: str
    follower_node_address: str
    api_url: str = "http://localhost:8000"
    timetagger_address: str = "127.0.0.1:8000"
    basis: list[float] = Field(default_factory=lambda: [0.0, 22.5])
    overall_timeout_s: int = 1800
    per_game_timeout_s: int = 600


class RNGSettings(BaseModel):
    channels: list[int] = Field(default_factory=lambda: [1, 2])
    fortune_size: int = 8


class CHSHSettings(BaseModel):
    # Specifies which half waveplate to use for the CHSH experiment. First value is the provider's name, second is the motor name.
    hwp: tuple[str, str] = ("", "")
    request_hwp: tuple[str, str] = ("", "")
    measurement_config: MeasurementConfig = Field(default_factory=lambda: MeasurementConfig(integration_time_s=5))
    expectation_signs: tuple[int, int, int, int] = (1, 1, 1, -1)


class QKDSettings(BaseModel):
    hwp: tuple[str, str] = ("", "")
    request_hwp: tuple[str, str] = ("", "")
    bitstring_length: int = 6
    minimum_question_index: int = 1
    maximum_question_index: int = 8
    discriminating_threshold: int = 10
    measurement_config: MeasurementConfig = Field(default_factory=lambda: MeasurementConfig(integration_time_s=5))


class GamesAvailability(BaseModel):
    chsh: bool = True  # "Verify Quantum Link"
    qf: bool = True  # "Quantum Fortune"
    ssm: bool = True  # "Share a Secret Message"


class Settings(BaseSettings):
    node_name: str = "node1"
    router_name: str = "router1"
    router_address: str = "localhost"
    router_port: int = 5555
    chsh_settings: CHSHSettings = CHSHSettings()
    qkd_settings: QKDSettings = QKDSettings()
    rng_settings: RNGSettings = RNGSettings()
    bell_state: BellState = BellState.Phi_plus
    daily_report: DailyReportConfig | None = None
    timetagger: tuple[str, str] | None = None  # Name of the timetagger to use for the CHSH experiment.
    rotary_encoder_address: str = "/dev/ttyACM0"
    virtual_rotator: bool = False  # If True, use terminal input instead of hardware rotary encoder
    games_availability: GamesAvailability = Field(default_factory=GamesAvailability)
    follower_node_address: str | None = None

    model_config = SettingsConfigDict(
        toml_file="./config.toml",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Allow extra fields in config.toml (e.g., daily_report)
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            TomlConfigSettingsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
            init_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def config_path() -> Path:
    """Return the file the settings above are loaded from."""
    # pydantic-settings types this as "one path, or a list of them, or None"; ours is one path.
    return Path(Settings.model_config["toml_file"])  # type: ignore[arg-type]


def write_config(path: Path, updates: Mapping[str, Any]) -> None:
    """Set keys in a config file, applying them to nothing.

    For editing a Node that is not running: the CLI can be pointed at any config file,
    and a Node need not be started from ``./config.toml``. A running Node calls
    ``update_config`` instead, so that its live settings match what was written.

    Two guarantees:

    - **Comments survive.** The file keeps its comments, key order, and whitespace;
      only the named keys change. Operators hand-write ``config.toml`` from a
      commented example, so a write that reformatted it would destroy their notes.
    - **The file is never left truncated.** Contents go to a temp file in the same
      directory and are renamed over the target, which is atomic on POSIX. A crash
      mid-write leaves the previous config intact.

    Parameters
    ----------
    path
        Config file to write. Created if missing, as are any missing tables in it.
    updates
        Dotted key path -> value, e.g. ``{"games_availability.qf": True}``.
    """
    document = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()

    for dotted_key, value in updates.items():
        *tables, leaf = dotted_key.split(".")
        node: Any = document
        for table in tables:
            if not isinstance(node.get(table), dict):
                if len(node) > 0:
                    node.add(tomlkit.nl())  # keep a new table from being jammed against the previous line
                node[table] = tomlkit.table()
            node = node[table]
        node[leaf] = value

    # Write beside the target and rename over it, which is atomic on POSIX.
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tomlkit.dumps(document))
            f.flush()
            os.fsync(f.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    logger.info("Updated %s: %s", path, ", ".join(f"{k}={v!r}" for k, v in updates.items()))


def update_config(updates: Mapping[str, Any]) -> None:
    """Set keys in this Node's own config file, and apply them to its live settings.

    The change takes effect immediately, so a running Node picks it up with no restart.
    Modules import the settings object once and hold it, so it is patched in place
    rather than rebuilt — replacing it would leave every module on a stale copy.

    Only leaf values are applied, by plain ``setattr``, so callers must pass values
    that already type-check for the target field — in practice they come from a
    validated model (see ``PUT /games/availability``).

    Persisting happens first: if the write fails, the in-memory state still matches
    what is on disk, which is the recoverable direction to fail in.
    """
    write_config(config_path(), updates)

    for dotted_key, value in updates.items():
        *attributes, leaf = dotted_key.split(".")
        target: Any = settings
        for attribute in attributes:
            target = getattr(target, attribute)
        setattr(target, leaf, value)


class NodeRole(Enum):
    """Enum indicating the role of this Node. Enum values are strings to see the role explicitly in logging instead of seeing numeric values."""

    INDEPENDENT = "independent"
    LEADER = "leader"
    FOLLOWER = "follower"


class NodeState(BaseModel):
    # Coordination state
    # FIXME: Make sure we are checking for the client_listening_for_follower_requests state everywhere.
    client_listening_for_follower_requests: bool = False

    # Current role of this node.
    role: NodeRole = NodeRole.INDEPENDENT
    # Address of the Node following this node
    followers_address: str = ""
    # Other node requested this node to follow it.
    following_requested: bool = False
    # User's response to the follow request. None if no response yet, True if accepted, False if rejected.
    following_requested_user_response: bool | None = None
    # The address of the leader this node is following. None if not following anyone.
    leaders_address: str = ""
    leaders_name: str = ""

    # CHSH state
    chsh_request_basis: list[float] = [22.5, 67.5]
    chsh_progress_current: int = 0  # Current iteration in CHSH measurement
    chsh_progress_total: int = 16  # Total iterations (2 basis x 2 follower x 2 angles x 2 perp)
    chsh_running: bool = False  # Whether CHSH measurement is currently running

    # QKD state
    # FIXME: At the moment the reset_coordination_state resets this, probably want to refactor that function out.
    qkd_question_order: list[int] = []  # Order of questions for QKD
    qkd_emoji_pick: str = ""  # Emoji chosen for QKD
    qkd_progress_current: int = 0  # Current iteration in QKD measurement
    qkd_progress_total: int = 11  # Total iterations (bitstring length)
    qkd_running: bool = False  # Whether QKD measurement is currently running
    qkd_leader_basis_list: list[QKDEncodingBasis] = [
        QKDEncodingBasis.DA,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.DA,
        QKDEncodingBasis.HV,
        QKDEncodingBasis.HV,
        QKDEncodingBasis.HV,
        QKDEncodingBasis.HV,
        QKDEncodingBasis.HV,
    ]
    qkd_follower_basis_list: list[QKDEncodingBasis] = []
    qkd_single_bit_current_index: int = 0  # Current index in follower basis list for single_bit endpoint
    qkd_bit_list: list[int] = []
    qkd_resulting_bit_list: list[int] = []  # Resulting bits after QKD
    qkd_request_basis_list: list[QKDEncodingBasis] = []  # Basis angles for QKD
    qkd_request_bit_list: list[int] = []
    qkd_n_matching_bits: int = -1  # Leaders populate this value after qkd is done. Same with the emoji

    # RNG state
    rng_progress_current: int = 0  # Current iteration in RNG fortune measurement
    rng_progress_total: int = 0  # Total iterations (fortune_size)
    rng_running: bool = False  # Whether RNG fortune measurement is currently running


state = NodeState()
ask_user_for_follow_event = asyncio.Event()
user_replied_event = asyncio.Event()
qkd_result_received_event = asyncio.Event()
protocol_cancelled_event = asyncio.Event()
chsh_progress_event = asyncio.Event()
rng_progress_event = asyncio.Event()


def get_state() -> NodeState:
    return state
