"""Tests for the Node API endpoints Whobot drives: node config, availability, operation mode, calibration, screenshot, reboot.

The reboot/screenshot tests only pin the *contract* — that reboot answers before
the machine dies, and that a missing `maim` is a clean error rather than a 500.
Whether the capture is a real desktop and whether the host comes back are
environmental and get verified on a live Node.
"""

from collections.abc import AsyncGenerator
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
import tomlkit
from fastapi.testclient import TestClient

from pqn_node.api import hardware
from pqn_node.api.deps import get_http_client
from pqn_node.api.routes import system
from pqn_node.api.routes.health import ComponentStatus
from pqn_node.api.routes.health import _last_probe
from pqn_node.constants import OperationMode
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import PolarizationAngles
from pqn_node.core.config import ReferenceRates
from pqn_node.core.config import settings
from pqn_node.main import app

UP = ComponentStatus(reachable=True)
DOWN = ComponentStatus(reachable=False, error="unreachable")

COMMENTED_CONFIG = """\
node_name = "example_node"  # trailing comment

# Router configuration
router_name = "router1"

[games_availability]
qf = true  # Quantum Fortune
"""

# Calibration values unlike any default, so a default that survived can never pass for a written one.
NEW_CALIBRATION = {
    "polarization_angles": {"h": 1.5, "v": 2.5, "d": 3.5, "a": 4.5},
    "reference_rates": {"vv": 11.0, "vd": 12.0, "dd": 13.0},
}

# The values a real calibration carries: A's angle is negative even at its default (-22.5), H back
# to zero, a fractional D, an angle above 90, DD back to its default rate, fractional rates. The
# negative and >1e6 rates are not physics; they pin that the Node stores exactly what it was given.
REALISTIC_CALIBRATION = {
    "polarization_angles": {"h": 0.0, "v": 112.5, "d": -21.37, "a": -22.5},
    "reference_rates": {"vv": 1234567.89, "vd": -13.5, "dd": 750.0},
}

# What a PUT leaves in config.toml when it started from COMMENTED_CONFIG: the operator's own keys,
# unchanged, plus exactly the keys the endpoint writes.
BASE_CONFIG = {
    "node_name": "example_node",
    "router_name": "router1",
    "games_availability": {"qf": True},
}


@pytest.fixture
def client() -> TestClient:
    # No `with` block: the lifespan startup health check probes real hardware.
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_global_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep availability, mode and calibration writes off the developer's real config.toml and settings."""
    monkeypatch.setattr("pqn_node.core.config.config_path", lambda: tmp_path / "config.toml")
    original_availability = settings.games_availability
    original_mode = settings.operation_mode
    original_angles = settings.polarization_angles
    original_rates = settings.correlation_settings.reference_rates
    # Copies, so in-place writes land on test objects, never on the real ones.
    settings.games_availability = original_availability.model_copy()
    settings.polarization_angles = original_angles.model_copy()
    settings.correlation_settings.reference_rates = original_rates.model_copy()
    original_probe = (_last_probe.ran, _last_probe.router, _last_probe.follower_node)
    yield
    settings.games_availability = original_availability
    settings.operation_mode = original_mode
    settings.polarization_angles = original_angles
    settings.correlation_settings.reference_rates = original_rates
    _last_probe.ran, _last_probe.router, _last_probe.follower_node = original_probe


@pytest.fixture
def commented_config(tmp_path: Path) -> Path:
    """Create the config.toml these writes land in, carrying the comments an operator wrote."""
    path = tmp_path / "config.toml"
    path.write_text(COMMENTED_CONFIG, encoding="utf-8")
    return path


def _plain_config(path: Path) -> dict[str, object]:
    """Read config.toml as a plain dict, so one exact comparison catches changed, dropped and added keys."""
    return dict(tomlkit.parse(path.read_text(encoding="utf-8")).unwrap())


def test_node_config_reports_the_node_name(client: TestClient) -> None:
    """Whobot reads each Node's name from the Node itself, not from its own registry."""
    response = client.get("/node/config")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["node_name"] == settings.node_name


def test_put_availability_persists_and_applies(client: TestClient, tmp_path: Path) -> None:
    _last_probe.ran, _last_probe.router, _last_probe.follower_node = True, UP, UP

    response = client.put("/games/availability", json={"chsh": True, "qf": False, "ssm": True})

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"chsh": True, "qf": False, "ssm": True}
    assert settings.games_availability.qf is False
    assert "qf = false" in (tmp_path / "config.toml").read_text(encoding="utf-8")


def test_put_availability_is_visible_to_get_without_a_restart(client: TestClient) -> None:
    """The acceptance criterion for the Whobot 'change game availability' capability."""
    _last_probe.ran, _last_probe.router, _last_probe.follower_node = True, UP, UP
    client.put("/games/availability", json={"chsh": True, "qf": True, "ssm": True})

    client.put("/games/availability", json={"chsh": False, "qf": True, "ssm": True})

    assert client.get("/games/availability").json() == {"chsh": False, "qf": True, "ssm": True}


def test_put_availability_cannot_enable_a_game_the_hardware_cannot_run(client: TestClient) -> None:
    """config.toml is a veto, never an override: unreachable hardware still wins."""
    _last_probe.ran, _last_probe.router, _last_probe.follower_node = True, DOWN, None

    response = client.put("/games/availability", json={"chsh": True, "qf": True, "ssm": True})

    assert response.json() == {"chsh": False, "qf": False, "ssm": False}
    # ...but the configured baseline was still recorded, so games come back with the hardware.
    assert settings.games_availability == GamesAvailability(chsh=True, qf=True, ssm=True)


# --------------------------------------------------------------------------------------
# /node/operation_mode and /node/calibration: settings Whobot can change while the Node runs.
# --------------------------------------------------------------------------------------


def test_get_operation_mode_reports_the_nodes_mode(client: TestClient) -> None:
    """Decision 8: the API carries the mode's own string, read from the live settings."""
    response = client.get("/node/operation_mode")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == settings.operation_mode


def test_put_operation_mode_persists_and_applies(client: TestClient, commented_config: Path) -> None:
    response = client.put("/node/operation_mode", json="correlation")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == "correlation"
    assert settings.operation_mode is OperationMode.CORRELATION
    written = commented_config.read_text(encoding="utf-8")
    assert 'operation_mode = "correlation"' in written
    assert "qf = true  # Quantum Fortune" in written  # the operator's comments survive the write


def test_put_operation_mode_is_visible_to_get_without_a_restart(client: TestClient) -> None:
    client.put("/node/operation_mode", json="correlation")

    assert client.get("/node/operation_mode").json() == "correlation"


def test_put_operation_mode_switches_to_correlation_and_back(client: TestClient, commented_config: Path) -> None:
    """A Node switched to correlation must be switchable back to entanglement, writing nothing else."""
    for mode in (OperationMode.CORRELATION, OperationMode.ENTANGLEMENT):
        response = client.put("/node/operation_mode", json=mode.value)

        assert response.status_code == HTTPStatus.OK
        assert response.json() == mode.value
        assert settings.operation_mode is mode
        assert client.get("/node/operation_mode").json() == mode.value
        assert _plain_config(commented_config) == {**BASE_CONFIG, "operation_mode": mode.value}


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param({"json": "quantum"}, id="an unknown mode"),
        pytest.param({"content": "null", "headers": {"content-type": "application/json"}}, id="a null body"),
    ],
)
def test_put_operation_mode_refusals_write_nothing(
    client: TestClient, commented_config: Path, request_kwargs: dict[str, object]
) -> None:
    """Decision 8: only "entanglement" and "correlation" are Operation modes, and a refusal writes nothing."""
    before = commented_config.read_text(encoding="utf-8")
    before_mode = settings.operation_mode

    response = client.put("/node/operation_mode", **request_kwargs)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert settings.operation_mode is before_mode
    assert commented_config.read_text(encoding="utf-8") == before


def test_get_calibration_reports_the_nodes_calibration(client: TestClient) -> None:
    response = client.get("/node/calibration")

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "polarization_angles": settings.polarization_angles.model_dump(),
        "reference_rates": settings.correlation_settings.reference_rates.model_dump(),
    }


def test_put_calibration_persists_and_applies(client: TestClient, commented_config: Path) -> None:
    held_angles = settings.polarization_angles
    held_rates = settings.correlation_settings.reference_rates

    response = client.put("/node/calibration", json=NEW_CALIBRATION)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == NEW_CALIBRATION
    # Applied live, in place: the settings object every module holds already carries the new values.
    assert settings.polarization_angles is held_angles
    assert held_angles == PolarizationAngles(h=1.5, v=2.5, d=3.5, a=4.5)
    assert settings.correlation_settings.reference_rates is held_rates
    assert held_rates == ReferenceRates(vv=11.0, vd=12.0, dd=13.0)
    written = commented_config.read_text(encoding="utf-8")
    assert "qf = true  # Quantum Fortune" in written  # the operator's comments survive the write
    document = tomlkit.parse(written)
    assert dict(document["polarization_angles"]) == NEW_CALIBRATION["polarization_angles"]
    assert dict(document["correlation_settings"]["reference_rates"]) == NEW_CALIBRATION["reference_rates"]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param(
            {
                "json": {
                    "polarization_angles": {"h": 1.5, "v": 2.5, "d": 3.5},  # a is missing
                    "reference_rates": {"vv": 11.0, "vd": 12.0, "dd": 13.0},
                }
            },
            id="an angle missing",
        ),
        pytest.param(
            {"json": {"polarization_angles": NEW_CALIBRATION["polarization_angles"]}}, id="reference_rates missing"
        ),
        pytest.param(
            {"json": {"reference_rates": NEW_CALIBRATION["reference_rates"]}}, id="polarization_angles missing"
        ),
        pytest.param({"json": {}}, id="an empty body"),
        pytest.param({}, id="no body at all"),
        pytest.param(
            {
                "json": {
                    "polarization_angles": NEW_CALIBRATION["polarization_angles"],
                    "reference_rates": {"vv": "eleven", "vd": 12.0, "dd": 13.0},
                }
            },
            id="a non-numeric rate",
        ),
    ],
)
def test_put_calibration_refuses_an_incomplete_body(
    client: TestClient, commented_config: Path, request_kwargs: dict[str, object]
) -> None:
    """Agent decision A4: a PUT states the whole calibration; anything less is a 422 that writes nothing."""
    before = commented_config.read_text(encoding="utf-8")
    before_angles = settings.polarization_angles.model_copy()
    before_rates = settings.correlation_settings.reference_rates.model_copy()

    response = client.put("/node/calibration", **request_kwargs)

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert commented_config.read_text(encoding="utf-8") == before
    assert settings.polarization_angles == before_angles
    assert settings.correlation_settings.reference_rates == before_rates


def test_put_calibration_round_trips_realistic_values(client: TestClient, commented_config: Path) -> None:
    """Round-trip the values a real calibration carries: negative, zero, fractional, above 90, and defaults.

    A's Calibrated angle is negative even at its default, rates are fractional in the wild, and an
    operator puts a value back to its default after tuning — so no value may be dropped, clamped,
    rounded or sign-flipped on its way into config.toml or back out of GET, and no other key may
    appear in the file. Every GET runs while the Node is in correlation mode, the mode the
    calibration serves.
    """
    assert client.put("/node/operation_mode", json="correlation").status_code == HTTPStatus.OK
    # Every value off its default, so the realistic values that follow are genuine changes.
    assert client.put("/node/calibration", json=NEW_CALIBRATION).status_code == HTTPStatus.OK
    assert client.get("/node/calibration").json() == NEW_CALIBRATION

    response = client.put("/node/calibration", json=REALISTIC_CALIBRATION)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == REALISTIC_CALIBRATION
    assert client.get("/node/calibration").json() == REALISTIC_CALIBRATION
    assert settings.polarization_angles == PolarizationAngles(**REALISTIC_CALIBRATION["polarization_angles"])
    assert settings.correlation_settings.reference_rates == ReferenceRates(**REALISTIC_CALIBRATION["reference_rates"])
    written = commented_config.read_text(encoding="utf-8")
    assert "qf = true  # Quantum Fortune" in written  # the operator's comments survive the write
    assert _plain_config(commented_config) == {
        **BASE_CONFIG,
        "operation_mode": "correlation",
        "polarization_angles": REALISTIC_CALIBRATION["polarization_angles"],
        "correlation_settings": {"reference_rates": REALISTIC_CALIBRATION["reference_rates"]},
    }


def test_put_calibration_is_visible_to_get_without_a_restart(client: TestClient) -> None:
    client.put("/node/calibration", json=NEW_CALIBRATION)

    assert client.get("/node/calibration").json() == NEW_CALIBRATION


def test_screenshot_returns_the_png_bytes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    png = b"\x89PNG\r\n\x1a\ncaptured"

    async def fake_run(_command: tuple[str, ...], _timeout: float) -> tuple[int, bytes, bytes]:
        return 0, png, b""

    monkeypatch.setattr(system, "_run", fake_run)

    response = client.get("/system/screenshot")

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"] == "image/png"
    assert response.content == png


def test_screenshot_without_maim_installed_is_a_clean_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(_command: tuple[str, ...], _timeout: float) -> tuple[int, bytes, bytes]:
        raise FileNotFoundError

    monkeypatch.setattr(system, "_run", fake_run)

    response = client.get("/system/screenshot")

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert "maim" in response.json()["detail"]


def test_screenshot_timeout_is_a_504(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(_command: tuple[str, ...], _timeout: float) -> tuple[int, bytes, bytes]:
        raise TimeoutError

    monkeypatch.setattr(system, "_run", fake_run)

    assert client.get("/system/screenshot").status_code == HTTPStatus.GATEWAY_TIMEOUT


def test_reboot_answers_before_rebooting(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller must get a response, not a dropped connection, so it can start polling."""
    rebooted = False

    async def fake_reboot(_delay_s: float) -> None:
        nonlocal rebooted
        rebooted = True

    monkeypatch.setattr(system, "_reboot_after_response", fake_reboot)

    response = client.post("/system/reboot")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["scheduled"] is True
    assert rebooted, "the reboot must be scheduled as a background task, after the response"


# --------------------------------------------------------------------------------------
# /chsh/sweep and /chsh/request-angle: the hardware and the network are faked.
# --------------------------------------------------------------------------------------


class FakeWaveplate:
    def __init__(self) -> None:
        self.moves: list[float] = []

    def move_to(self, angle: float) -> None:
        self.moves.append(angle)


@pytest.fixture
def waveplate(monkeypatch: pytest.MonkeyPatch) -> FakeWaveplate:
    plate = FakeWaveplate()

    class FakeRouterClient:
        def __init__(self, **_: object) -> None:
            pass

        def get_device(self, *_: object) -> FakeWaveplate:
            return plate

    monkeypatch.setattr(hardware, "Client", FakeRouterClient)
    return plate


@pytest.fixture
def outbound() -> Iterator[list[httpx.Request]]:
    """Record the leader's calls to the follower and the timetagger, answering each one."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/timetagger/measure_correlation":
            return httpx.Response(200, json=len(seen))
        if request.url.path == "/node/config":
            return httpx.Response(200, json={"node_name": "bob", "follower_node_address": None})
        return httpx.Response(200, json=True)

    async def fake_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = fake_http_client
    yield seen
    app.dependency_overrides.pop(get_http_client, None)


def test_request_angle_moves_the_requested_plate_to_half_the_angle(
    client: TestClient, waveplate: FakeWaveplate
) -> None:
    """A custom angle, one no named polarization sits at, moves raw: half the polarization angle."""
    response = client.post("/chsh/request-angle", params={"angle": 37.5})

    assert response.status_code == HTTPStatus.OK
    assert waveplate.moves == [18.75]


def test_request_angle_by_basis_moves_to_half_the_basis_angle_plus_90_when_perp(
    client: TestClient, waveplate: FakeWaveplate
) -> None:
    for index, perp in [(0, False), (1, True)]:
        response = client.post("/chsh/request-angle-by-basis", params={"index": index, "perp": perp})
        assert response.status_code == HTTPStatus.OK

    assert waveplate.moves == [22.5 / 2, (67.5 + 90) / 2]


def test_sweep_pins_the_follower_once_and_measures_every_step(
    client: TestClient,
    waveplate: FakeWaveplate,
    outbound: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "follower_node_address", "follower.invalid:8000")

    response = client.post(
        "/chsh/sweep",
        params={"timetagger_address": "tt.invalid:9000", "follower_angle": 30.0, "step_deg": 90.0},
    )

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["angles"] == [0.0, 90.0, 180.0, 270.0]
    assert body["leader_name"] == settings.node_name
    assert body["follower_name"] == "bob"
    assert body["step_deg"] == 90.0  # noqa: PLR2004
    assert body["integration_time_s"] == 1.0
    assert len(body["coincidences"]) == len(body["angles"])
    assert waveplate.moves == [0.0, 45.0, 90.0, 135.0]
    follower_calls = [r for r in outbound if r.url.host == "follower.invalid" and r.url.path != "/node/config"]
    assert len(follower_calls) == 1
    assert follower_calls[0].url.path == "/chsh/request-angle"
    assert follower_calls[0].url.params["angle"] == "30.0"


def test_sweep_without_a_follower_is_a_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "follower_node_address", None)

    response = client.post("/chsh/sweep", params={"timetagger_address": "tt.invalid:9000", "follower_angle": 0.0})

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_sweep_rejects_a_step_that_is_not_positive(client: TestClient) -> None:
    response = client.post(
        "/chsh/sweep", params={"timetagger_address": "tt.invalid:9000", "follower_angle": 0.0, "step_deg": 0}
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_a_follower_that_will_not_say_its_name_is_named_by_its_address(
    client: TestClient, waveplate: FakeWaveplate, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/node/config":
            return httpx.Response(404)
        return httpx.Response(200, json=7)

    async def fake_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            yield http_client

    monkeypatch.setattr(settings, "follower_node_address", "follower.invalid:8000")
    app.dependency_overrides[get_http_client] = fake_http_client
    try:
        response = client.post(
            "/chsh/sweep",
            params={"timetagger_address": "tt.invalid:9000", "follower_angle": 0.0, "step_deg": 180.0},
        )
    finally:
        app.dependency_overrides.pop(get_http_client, None)

    assert response.status_code == HTTPStatus.OK
    assert response.json()["follower_name"] == "follower.invalid:8000"
    assert waveplate.moves == [0.0, 90.0]
