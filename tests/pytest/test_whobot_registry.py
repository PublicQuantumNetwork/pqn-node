"""Tests for the Node Registry and the Node API client, against a mocked Node API.

What is pinned: names come from each Node, an unreachable Node is a result rather than an
exception, and no bad address can hang or crash a listing. All of it runs off
``httpx.MockTransport`` — no Node, no network.
"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from pqn_whobot.config import WhobotSettings
from pqn_whobot.node_client import NodeApiError
from pqn_whobot.node_client import NodeClient
from pqn_whobot.registry import UNKNOWN_NAME
from pqn_whobot.registry import Node
from pqn_whobot.registry import resolve_node
from pqn_whobot.registry import resolve_nodes

ALICE = "http://node-a.invalid:9000"
BOB = "http://node-b.invalid:9000"
DEAD = "http://offline.invalid:9000"

NAMES = {ALICE: "uiuc-public-left", BOB: "ufl-public-right"}


def node_api(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _by_name(request: httpx.Request) -> httpx.Response:
    """Answer /node/config for the Nodes this fake knows about, and refuse every other address."""
    origin = f"{request.url.scheme}://{request.url.netloc.decode()}"
    if request.url.path != "/node/config":
        return httpx.Response(404)
    if origin not in NAMES:
        msg = "Connection refused"
        raise httpx.ConnectError(msg, request=request)
    return httpx.Response(200, json={"node_name": NAMES[origin], "follower_node_address": None})


@pytest.fixture(autouse=True)
def _in_a_temp_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whobot reads `./whobot.toml`, so each test gets a directory of its own to hold one."""
    monkeypatch.chdir(tmp_path)


def settings_for(*api_urls: str, reachability_timeout_s: float = 5.0) -> WhobotSettings:
    """Write a registry of these addresses to `whobot.toml` and load it the way Whobot does."""
    body = f"reachability_timeout_s = {reachability_timeout_s}\n"
    body += "".join(f'\n[[nodes]]\napi_url = "{api_url}"\n' for api_url in api_urls)
    Path("whobot.toml").write_text(body, encoding="utf-8")
    return WhobotSettings()


def resolve_all(settings: WhobotSettings, transport: httpx.MockTransport) -> list[Node]:
    """Resolve a registry with every client's transport swapped for the mock."""

    async def run() -> list[Node]:
        clients = [NodeClient(entry.api_url, transport) for entry in settings.nodes]
        return list(await asyncio.gather(*(resolve_node(c, settings.reachability_timeout_s) for c in clients)))

    return asyncio.run(run())


def test_names_come_from_each_node_not_from_the_registry() -> None:
    """The registry holds addresses; the name comes from the Node."""
    resolved = resolve_all(settings_for(ALICE, BOB), node_api(_by_name))

    assert [(node.name, node.api_url) for node in resolved] == [
        ("uiuc-public-left", ALICE),
        ("ufl-public-right", BOB),
    ]
    assert all(node.reachable for node in resolved)
    assert all(node.error is None for node in resolved)
    assert all(node.latency_ms is not None for node in resolved)


def test_registry_order_is_preserved() -> None:
    """Resolution is concurrent, so the answers must be re-ordered back to config order."""
    resolved = resolve_all(settings_for(BOB, ALICE), node_api(_by_name))

    assert [node.api_url for node in resolved] == [BOB, ALICE]


def test_an_unreachable_node_has_no_name() -> None:
    """A Node that won't answer never said what it is called, so it is called unknown."""
    resolved = resolve_all(settings_for(DEAD), node_api(_by_name))

    assert resolved[0].reachable is False
    assert resolved[0].name == UNKNOWN_NAME
    assert resolved[0].error is not None
    assert "ConnectError" in resolved[0].error


def test_one_dead_node_does_not_hide_the_healthy_ones() -> None:
    resolved = resolve_all(settings_for(ALICE, DEAD, BOB), node_api(_by_name))

    assert [node.reachable for node in resolved] == [True, False, True]
    assert [node.name for node in resolved] == ["uiuc-public-left", UNKNOWN_NAME, "ufl-public-right"]


def test_an_empty_registry_resolves_to_nothing() -> None:
    assert asyncio.run(resolve_nodes(settings_for())) == []


def test_an_http_error_is_reported_not_raised() -> None:
    """A Node that answers with a 500 is as unusable as one that doesn't answer at all."""
    resolved = resolve_all(settings_for(ALICE), node_api(lambda _request: httpx.Response(500)))

    assert resolved[0].reachable is False
    assert "500" in str(resolved[0].error)


def test_a_non_node_answering_the_address_is_reported() -> None:
    """Something else on that port must read as unreachable, not crash the listing."""
    resolved = resolve_all(settings_for(ALICE), node_api(lambda _request: httpx.Response(200, text="<html>hi</html>")))

    assert resolved[0].reachable is False
    assert "did not return JSON" in str(resolved[0].error)


def test_a_node_without_node_name_is_reachable_but_warned() -> None:
    """A Node from before `node_name` was added to /node/config is out of date, not down.

    It answers, it is on the network, and it can still be reached — reporting it as
    unreachable would send an operator to look for a network fault that isn't there.
    """
    old_node = lambda _request: httpx.Response(200, json={"follower_node_address": None})  # noqa: E731

    resolved = resolve_all(settings_for(ALICE), node_api(old_node))

    assert resolved[0].reachable is True
    assert resolved[0].error is None
    assert resolved[0].name == UNKNOWN_NAME
    assert resolved[0].warning is not None
    assert "node_name" in resolved[0].warning


def test_json_without_any_expected_field_is_not_a_node() -> None:
    """Both fields are optional, so this is what stops any JSON server passing for a Node."""
    resolved = resolve_all(settings_for(ALICE), node_api(lambda _request: httpx.Response(200, json={})))

    assert resolved[0].reachable is False
    assert "is not a Node's config" in str(resolved[0].error)


def test_a_hanging_node_times_out_rather_than_hanging() -> None:
    """`reachability_timeout_s` bounds the wait on a Node that accepts but never answers."""

    def hangs(request: httpx.Request) -> httpx.Response:
        msg = "timed out"
        raise httpx.ReadTimeout(msg, request=request)

    resolved = resolve_all(settings_for(ALICE, reachability_timeout_s=0.01), node_api(hangs))

    assert resolved[0].reachable is False
    assert "ReadTimeout" in str(resolved[0].error)


def test_the_client_reads_the_nodes_own_config() -> None:
    client = NodeClient(ALICE, transport=node_api(_by_name))

    config = asyncio.run(client.get_config(5.0))

    assert config.node_name == "uiuc-public-left"
    assert config.follower_node_address is None


def test_the_client_raises_one_error_type_for_every_failure() -> None:
    client = NodeClient(DEAD, transport=node_api(_by_name))

    with pytest.raises(NodeApiError):
        asyncio.run(client.get_config(5.0))


def test_the_client_normalises_a_trailing_slash() -> None:
    """So paths can be appended without producing a double slash."""
    assert NodeClient(f"{ALICE}/").api_url == ALICE


# The three calls the digest makes: a hardware probe, and the two Games it runs for real.

HEALTHY = {
    "router": {"reachable": True, "latency_ms": 3.2},
    "devices": [{"reachable": True, "provider": "prov", "name": "tagger", "purpose": "counting", "latency_ms": 8.0}],
    "rotary_encoder": None,
    "follower_node": {"reachable": True, "latency_ms": 12.0},
}

CHSH = {
    "chsh_value": 2.61,
    "chsh_error": 0.04,
    "expectation_values": [0.7, -0.65, 0.66, 0.6],
    "expectation_errors": [0.01, 0.01, 0.01, 0.01],
    "expectation_values_sign_fixed": [0.7, -0.65, 0.66, -0.6],
}


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> NodeClient:
    return NodeClient(ALICE, transport=node_api(handler))


def test_the_client_reads_a_hardware_health_probe() -> None:
    """`GET /health/` takes no parameters — the Node reads its follower's address itself."""
    seen: list[httpx.Request] = []

    def probe(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=HEALTHY)

    health = asyncio.run(client_for(probe).get_health(30.0))

    assert seen[0].url.path == "/health/"
    assert not seen[0].url.params
    assert health.all_ok is True
    assert health.devices[0].purpose == "counting"


def test_running_chsh_sends_the_basis_as_the_body_and_the_timetagger_as_a_parameter() -> None:
    """Which is what the endpoint's own signature asks for; getting it wrong is a 422."""
    seen: list[httpx.Request] = []

    def measure(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CHSH)

    result = asyncio.run(client_for(measure).run_chsh("10.0.0.5:9000", (0.0, 22.5), 30.0))

    assert seen[0].method == "POST"
    assert seen[0].url.path == "/chsh/"
    assert seen[0].url.params["timetagger_address"] == "10.0.0.5:9000"
    assert json.loads(seen[0].content) == [0.0, 22.5]
    assert result.chsh_value == pytest.approx(2.61)


def test_running_a_fortune_leaves_the_nodes_own_calibration_alone() -> None:
    """`fortune_size` and `channels` are the Node's to choose; an unattended run must not override them."""
    seen: list[httpx.Request] = []

    def draw(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[42, 137])

    drawn = asyncio.run(client_for(draw).run_fortune("10.0.0.5:9000", 30.0))

    assert dict(seen[0].url.params) == {"timetagger_address": "10.0.0.5:9000"}
    assert drawn == [42, 137]


@pytest.mark.parametrize(
    ("call", "payload", "expected"),
    [
        ("get_health", {"nothing": "expected"}, "did not answer with a health status"),
        ("run_chsh", {"chsh_value": "not a number"}, "did not answer with a CHSH result"),
        ("run_fortune", {"fortune": [1, 2]}, "is not a fortune per channel"),
        ("run_fortune", ["not", "numbers"], "is not a fortune per channel"),
    ],
)
def test_a_node_answering_with_the_wrong_shape_is_one_error_type(call: str, payload: object, expected: str) -> None:
    """A 200 of the wrong shape is as unusable as a refusal, and must read like one."""
    client = client_for(lambda _request: httpx.Response(200, json=payload))
    arguments: dict[str, tuple[object, ...]] = {
        "get_health": (30.0,),
        "run_chsh": ("10.0.0.5:9000", (0.0, 22.5), 30.0),
        "run_fortune": ("10.0.0.5:9000", 30.0),
    }

    with pytest.raises(NodeApiError, match=expected):
        asyncio.run(getattr(client, call)(*arguments[call]))


def test_a_game_that_fails_on_the_node_carries_the_nodes_own_reason() -> None:
    """So Slack shows what the Node said, rather than sending an operator to its logs."""
    refused = lambda _request: httpx.Response(503, json={"detail": "follower_node_address not configured"})  # noqa: E731

    with pytest.raises(NodeApiError, match="follower_node_address not configured"):
        asyncio.run(client_for(refused).run_chsh("10.0.0.5:9000", (0.0, 22.5), 30.0))
