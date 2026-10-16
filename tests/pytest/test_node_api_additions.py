"""Tests for the Node API endpoints Whobot drives: node config, availability, screenshot, reboot.

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
from fastapi.testclient import TestClient

from pqn_node.api import deps
from pqn_node.api.deps import get_http_client
from pqn_node.api.routes import system
from pqn_node.api.routes.health import ComponentStatus
from pqn_node.api.routes.health import _last_probe
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import settings
from pqn_node.main import app

UP = ComponentStatus(reachable=True)
DOWN = ComponentStatus(reachable=False, error="unreachable")


@pytest.fixture
def client() -> TestClient:
    # No `with` block: the lifespan startup health check probes real hardware.
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_global_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep availability writes off the developer's real config.toml and settings."""
    monkeypatch.setattr("pqn_node.core.config.config_path", lambda: tmp_path / "config.toml")
    original_availability = settings.games_availability
    settings.games_availability = original_availability.model_copy()
    original_probe = (_last_probe.ran, _last_probe.router, _last_probe.follower_node)
    yield
    settings.games_availability = original_availability
    _last_probe.ran, _last_probe.router, _last_probe.follower_node = original_probe


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

    monkeypatch.setattr(deps, "Client", FakeRouterClient)
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
    response = client.post("/chsh/request-angle", params={"angle": 45.0})

    assert response.status_code == HTTPStatus.OK
    assert waveplate.moves == [22.5]


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
