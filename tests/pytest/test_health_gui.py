"""Tests for the GUI probe in `/health`.

The GUI is reported but is deliberately not an availability gate input: a dead
GUI must not switch games off. A local stub HTTP server stands in for pqn-gui.
"""

import socket
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer

import pytest
from fastapi.testclient import TestClient

from pqn_node.api.routes import health as health_module
from pqn_node.api.routes.health import ComponentStatus
from pqn_node.api.routes.health import _last_probe
from pqn_node.api.routes.health import _probe_gui
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import settings
from pqn_node.main import app


def _serve(status: HTTPStatus) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def gui_up() -> Iterator[str]:
    yield from _serve(HTTPStatus.OK)


@pytest.fixture
def gui_erroring() -> Iterator[str]:
    yield from _serve(HTTPStatus.INTERNAL_SERVER_ERROR)


@pytest.fixture
def gui_down() -> str:
    """Return an address nothing listens on, by binding a port and releasing it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"127.0.0.1:{port}"


@pytest.fixture(autouse=True)
def _healthy_hardware(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Everything but the GUI is up, without touching real hardware."""
    monkeypatch.setattr(health_module, "_connect_router", lambda: (ComponentStatus(reachable=True), None))
    monkeypatch.setattr(health_module, "_configured_devices", list)
    monkeypatch.setattr(settings, "virtual_rotator", True)
    monkeypatch.setattr(settings, "follower_node_address", None)
    monkeypatch.setattr(settings, "games_availability", GamesAvailability(chsh=True, qf=True, ssm=True))
    original_probe = (_last_probe.ran, _last_probe.router, _last_probe.follower_node)
    yield
    _last_probe.ran, _last_probe.router, _last_probe.follower_node = original_probe


def test_probe_gui_up_is_reachable(gui_up: str) -> None:
    status = _probe_gui(gui_up)

    assert status.reachable is True
    assert status.error is None
    assert status.latency_ms is not None


def test_probe_gui_down_is_unreachable_with_an_error(gui_down: str) -> None:
    status = _probe_gui(gui_down)

    assert status.reachable is False
    assert status.error


def test_probe_gui_non_2xx_counts_as_down(gui_erroring: str) -> None:
    status = _probe_gui(gui_erroring)

    assert status.reachable is False
    assert "500" in (status.error or "")


def test_health_reports_the_gui(monkeypatch: pytest.MonkeyPatch, gui_up: str) -> None:
    monkeypatch.setattr(settings, "gui_address", gui_up)

    status = health_module.health()

    assert status.gui is not None
    assert status.gui.reachable is True


@pytest.mark.parametrize("opt_out", ["", None])
def test_health_opted_out_does_not_probe_the_gui(monkeypatch: pytest.MonkeyPatch, opt_out: str | None) -> None:
    monkeypatch.setattr(settings, "gui_address", opt_out)

    assert health_module.health().gui is None


def test_dead_gui_does_not_gate_availability(monkeypatch: pytest.MonkeyPatch, gui_down: str) -> None:
    """Decision 5: the GUI is not an Availability gate input."""
    monkeypatch.setattr(settings, "gui_address", gui_down)

    status = health_module.health()

    assert status.gui is not None
    assert status.gui.reachable is False
    assert status.all_ok is True
    # No `with` block: the lifespan startup health check probes real hardware.
    response = TestClient(app).get("/games/availability")
    assert response.json() == {"chsh": True, "qf": True, "ssm": True}
