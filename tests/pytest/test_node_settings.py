"""Tests for the operation-mode Node settings: the Operation mode, Calibrated angles and Reference rates.

The three guarantees of plan group 1.1: every new key written by ``update_config`` survives with
the comments kept and is applied live, a config without the new keys behaves exactly as today,
and every move to a named polarization goes to its Calibrated angle while continuous angles keep
moving raw.
"""

import json
from collections.abc import AsyncGenerator
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import httpx
import pytest
import tomlkit
from fastapi.testclient import TestClient

from pqn_node.api import hardware
from pqn_node.api.deps import get_http_client
from pqn_node.api.routes import qkd
from pqn_node.constants import OperationMode
from pqn_node.constants import Polarization
from pqn_node.constants import QKDAngleValuesHWP
from pqn_node.constants import QKDEncodingBasis
from pqn_node.core import config
from pqn_node.core.config import CorrelationSettings
from pqn_node.core.config import NodeState
from pqn_node.core.config import PolarizationAngles
from pqn_node.core.config import ReferenceRates
from pqn_node.core.config import Settings
from pqn_node.core.config import get_state
from pqn_node.core.config import settings
from pqn_node.core.config import update_config
from pqn_node.main import app

EXAMPLE_CONFIG = """\
node_name = "example_node"  # trailing comment

# Router configuration
router_name = "router1"

[games_availability]
qf = true  # Quantum Fortune
"""

TODAYS_CONFIG = """\
node_name = "example_node"

[qkd_settings]
hwp = ["provider", "instrument_hwp"]
request_hwp = ["provider", "instrument_hwp"]
"""

CONFIG_EXAMPLE = Path(__file__).parents[2] / "configs" / "config_example.toml"

TODAYS_ANGLES = PolarizationAngles(
    h=float(QKDAngleValuesHWP.H.value),
    v=float(QKDAngleValuesHWP.V.value),
    d=float(QKDAngleValuesHWP.D.value),
    a=float(QKDAngleValuesHWP.A.value),
)

WRITTEN_ANGLES = {"h": 1.5, "v": 2.5, "d": 3.5, "a": 4.5}
WRITTEN_RATES = {"vv": 11.0, "vd": 12.0, "dd": 13.0}
NEW_KEYS = {
    "operation_mode": OperationMode.CORRELATION,
    **{f"polarization_angles.{name}": angle for name, angle in WRITTEN_ANGLES.items()},
    **{f"correlation_settings.reference_rates.{name}": rate for name, rate in WRITTEN_RATES.items()},
}

# Calibrated angles a little off today's raw ones, so a raw move can never pass for a calibrated one.
CALIBRATED = {"h": 3.0, "v": 44.0, "d": 20.0, "a": -20.0}


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return path


class _FakeSettings:
    """Stand-in for the live settings singleton, holding just what these tests write to."""

    def __init__(self) -> None:
        self.operation_mode = OperationMode.ENTANGLEMENT
        self.polarization_angles = PolarizationAngles()
        self.correlation_settings = CorrelationSettings()


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> _FakeSettings:
    """Swap out the live settings singleton, so no test can apply changes to the real one."""
    fake = _FakeSettings()
    monkeypatch.setattr(config, "settings", fake)
    return fake


@pytest.fixture(autouse=True)
def calibrated(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Set the Calibrated angles on the live settings, restored afterwards."""
    for name, angle in CALIBRATED.items():
        monkeypatch.setattr(settings.polarization_angles, name, angle)
    return CALIBRATED


# --------------------------------------------------------------------------------------
# update_config: every new key survives the write, with comments, and applies live.
# --------------------------------------------------------------------------------------


def test_update_config_keeps_the_comments_around_the_new_keys(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "config_path", lambda: config_file)

    update_config(NEW_KEYS)

    written = config_file.read_text(encoding="utf-8")
    assert "# Router configuration" in written
    assert 'node_name = "example_node"  # trailing comment' in written
    assert "qf = true  # Quantum Fortune" in written


def test_every_new_key_round_trips_through_the_file(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "config_path", lambda: config_file)

    update_config(NEW_KEYS)

    document = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    assert document["operation_mode"] == NEW_KEYS["operation_mode"].value
    assert dict(document["polarization_angles"]) == WRITTEN_ANGLES
    assert dict(document["correlation_settings"]["reference_rates"]) == WRITTEN_RATES


def test_every_new_key_is_applied_live(
    config_file: Path, fake_settings: _FakeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modules import `settings` once and hold it, so the change must land on that object."""
    monkeypatch.setattr(config, "config_path", lambda: config_file)
    held_angles = fake_settings.polarization_angles
    held_rates = fake_settings.correlation_settings.reference_rates

    update_config(NEW_KEYS)

    assert fake_settings.operation_mode is OperationMode.CORRELATION
    assert fake_settings.polarization_angles is held_angles
    assert held_angles == PolarizationAngles(**WRITTEN_ANGLES)
    assert fake_settings.correlation_settings.reference_rates is held_rates
    assert held_rates == ReferenceRates(**WRITTEN_RATES)


def test_what_update_config_writes_a_restarted_node_loads(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is the contract: a fresh Settings must load everything update_config wrote."""
    monkeypatch.setattr(config, "config_path", lambda: config_file)
    update_config(NEW_KEYS)

    monkeypatch.chdir(tmp_path)
    loaded = Settings()

    assert loaded.operation_mode is OperationMode.CORRELATION
    assert loaded.polarization_angles == PolarizationAngles(**WRITTEN_ANGLES)
    assert loaded.correlation_settings.reference_rates == ReferenceRates(**WRITTEN_RATES)


def test_a_config_without_the_new_keys_behaves_as_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Node that upgrades without touching its config must keep today's angles and Operation mode."""
    (tmp_path / "config.toml").write_text(TODAYS_CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    loaded = Settings()

    assert loaded.operation_mode is OperationMode.ENTANGLEMENT
    assert loaded.polarization_angles == TODAYS_ANGLES


def test_the_shipped_example_config_loads_with_the_documented_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The example operators copy must load, and it ships the documented defaults (agent decision A2)."""
    (tmp_path / "config.toml").write_text(CONFIG_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    loaded = Settings()

    assert loaded.operation_mode is OperationMode.ENTANGLEMENT
    assert loaded.polarization_angles == TODAYS_ANGLES
    assert loaded.correlation_settings.reference_rates == ReferenceRates(vv=3000.0, vd=1500.0, dd=750.0)


def test_the_correlation_mode_value_loads_from_its_literal_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 8: the value strings are the API and Whobot contract, and a config writes them literally.

    "entanglement" is already pinned by the example-config test, which loads it from the shipped file.
    """
    (tmp_path / "config.toml").write_text('operation_mode = "correlation"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert Settings().operation_mode is OperationMode.CORRELATION


def test_the_reference_rate_defaults_expect_a_product_vv_source() -> None:
    """Agent decision A2: VV 1, VD 1/2, DD 1/4 at ~3000 coincidences/s, until an operator sets real ones."""
    rates = ReferenceRates()

    assert (rates.vv, rates.vd, rates.dd) == (3000.0, 1500.0, 750.0)


# --------------------------------------------------------------------------------------
# The moves: a fake waveplate through hardware.Client, as the sweep tests do.
# --------------------------------------------------------------------------------------


class FakeWaveplate:
    def __init__(self) -> None:
        self.moves: list[float] = []

    def move_to(self, angle: float) -> None:
        self.moves.append(angle)


class FakeNode:
    """A Node whose router serves one fake waveplate and whose follower and timetagger answer over HTTP."""

    def __init__(self, client: TestClient, waveplate: FakeWaveplate) -> None:
        self.client = client
        self.waveplate = waveplate


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNode]:
    """Wire the app to a fake waveplate and a mock follower and timetagger, as the leader in a game."""
    plate = FakeWaveplate()

    class FakeRouterClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_device(self, *_: object) -> FakeWaveplate:
            return plate

    monkeypatch.setattr(hardware, "Client", FakeRouterClient)
    monkeypatch.setattr(settings, "follower_node_address", "follower.invalid:8000")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/timetagger/measure_correlation":
            return httpx.Response(200, json=42)
        if request.url.path == "/qkd/request_basis_list":
            return httpx.Response(200, json=json.loads(request.read()))
        if request.url.path == "/node/config":
            return httpx.Response(200, json={"node_name": "bob", "follower_node_address": None})
        return httpx.Response(200, json=True)

    async def fake_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = fake_http_client
    # No `with` block on the TestClient: the lifespan startup health check probes real hardware.
    yield FakeNode(TestClient(app), plate)
    app.dependency_overrides.pop(get_http_client, None)


@pytest.fixture
def qkd_state() -> Iterator[NodeState]:
    """Borrow the module-global QKD state fields, cleared, and put the originals back."""
    state = get_state()
    saved = (
        state.qkd_leader_basis_list,
        state.qkd_bit_list,
        state.qkd_follower_basis_list,
        state.qkd_request_basis_list,
        state.qkd_request_bit_list,
        state.qkd_single_bit_current_index,
    )
    state.qkd_leader_basis_list = []
    state.qkd_bit_list = []
    state.qkd_follower_basis_list = []
    state.qkd_request_basis_list = []
    state.qkd_request_bit_list = []
    state.qkd_single_bit_current_index = 0
    yield state
    (
        state.qkd_leader_basis_list,
        state.qkd_bit_list,
        state.qkd_follower_basis_list,
        state.qkd_request_basis_list,
        state.qkd_request_bit_list,
        state.qkd_single_bit_current_index,
    ) = saved


@pytest.mark.parametrize("named", [Polarization.H, Polarization.V, Polarization.D, Polarization.A])
def test_a_named_pin_moves_the_follower_to_its_calibrated_angle(
    node: FakeNode, calibrated: dict[str, float], named: Polarization
) -> None:
    """The Angle Sweep's follower pin arrives as a nominal angle and moves to the Calibrated one."""
    response = node.client.post("/chsh/request-angle", params={"angle": named.angle})

    assert response.status_code == HTTPStatus.OK
    assert response.json() is True
    assert node.waveplate.moves == [calibrated[named.value.lower()]]


@pytest.mark.parametrize(("nominal", "name"), [(0.0, "h"), (45.0, "d"), (90.0, "v"), (315.0, "a")])
def test_a_custom_pin_at_a_nominal_angle_still_moves_calibrated(
    node: FakeNode, calibrated: dict[str, float], nominal: float, name: str
) -> None:
    """A "custom" pin at exactly a nominal angle still moves to the Calibrated angle.

    Agent decision A3: the endpoint only receives a float, so a pin at 0, 45, 90 or 315 degrees —
    the nominal H/V/D/A polarization angles — is that named polarization, whatever the form sent.
    """
    response = node.client.post("/chsh/request-angle", params={"angle": nominal})

    assert response.status_code == HTTPStatus.OK
    assert node.waveplate.moves == [calibrated[name]]


def test_a_custom_pin_keeps_moving_raw(node: FakeNode) -> None:
    """A custom angle is not one of the four named polarizations, so it moves at half the angle."""
    response = node.client.post("/chsh/request-angle", params={"angle": 37.5})

    assert response.status_code == HTTPStatus.OK
    assert response.json() is True
    assert node.waveplate.moves == [18.75]


def test_a_chsh_follower_move_keeps_moving_raw(node: FakeNode) -> None:
    """CHSH's follower moves are continuous basis angles, not moves to a named polarization."""
    for index, perp in [(0, False), (1, True)]:
        response = node.client.post("/chsh/request-angle-by-basis", params={"index": index, "perp": perp})
        assert response.status_code == HTTPStatus.OK

    assert node.waveplate.moves == [22.5 / 2, (67.5 + 90) / 2]


def test_the_sweeps_leader_steps_keep_moving_raw(node: FakeNode) -> None:
    """Decision 6: only moves to a *named* polarization are calibrated; the sweep's steps are continuous."""
    response = node.client.post(
        "/chsh/sweep",
        params={"timetagger_address": "tt.invalid:9000", "follower_angle": 30.0, "step_deg": 90.0},
    )

    assert response.status_code == HTTPStatus.OK
    assert node.waveplate.moves == [0.0, 45.0, 90.0, 135.0]


class AnswerCase(NamedTuple):
    """One SSM question on one side: the basis an Answer picks, the random bit within it, and its angle."""

    basis: QKDEncodingBasis
    choice: int
    name: str


ANSWER_CASES = [
    AnswerCase(QKDEncodingBasis.HV, 0, "h"),
    AnswerCase(QKDEncodingBasis.HV, 1, "v"),
    AnswerCase(QKDEncodingBasis.DA, 0, "d"),
    AnswerCase(QKDEncodingBasis.DA, 1, "a"),
]


@pytest.mark.parametrize("case", ANSWER_CASES)
def test_the_ssm_leader_moves_to_the_calibrated_angle(
    node: FakeNode,
    calibrated: dict[str, float],
    qkd_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    case: AnswerCase,
) -> None:
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=lambda _bits: case.choice))
    qkd_state.qkd_leader_basis_list = [case.basis]

    response = node.client.post("/qkd", params={"timetagger_address": "tt.invalid:9000"})

    assert response.status_code == HTTPStatus.OK
    assert node.waveplate.moves == [calibrated[case.name]]


@pytest.mark.parametrize("case", ANSWER_CASES)
def test_the_ssm_follower_moves_to_the_calibrated_angle(
    node: FakeNode,
    calibrated: dict[str, float],
    qkd_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    case: AnswerCase,
) -> None:
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=lambda _bits: case.choice))
    qkd_state.qkd_follower_basis_list = [case.basis]

    response = node.client.post("/qkd/single_bit")

    assert response.status_code == HTTPStatus.OK
    assert node.waveplate.moves == [calibrated[case.name]]
