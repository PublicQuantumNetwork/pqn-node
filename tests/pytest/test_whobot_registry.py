"""Tests for the Node Registry and the Node API client, against a mocked Node API.

What is pinned: names come from each Node, an unreachable Node is a result rather than an
exception, and no bad address can hang or crash a listing. All of it runs off
``httpx.MockTransport`` — no Node, no network.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from pqn_whobot.config import WhobotSettings
from pqn_whobot.node_client import NodeApiError
from pqn_whobot.node_client import NodeClient
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
        clients = [NodeClient(entry.api_url, settings.reachability_timeout_s, transport) for entry in settings.nodes]
        return list(await asyncio.gather(*(resolve_node(client) for client in clients)))

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
    """A Node that won't answer never said what it is called."""
    resolved = resolve_all(settings_for(DEAD), node_api(_by_name))

    assert resolved[0].reachable is False
    assert resolved[0].name is None
    assert resolved[0].error is not None
    assert "ConnectError" in resolved[0].error


def test_one_dead_node_does_not_hide_the_healthy_ones() -> None:
    resolved = resolve_all(settings_for(ALICE, DEAD, BOB), node_api(_by_name))

    assert [node.reachable for node in resolved] == [True, False, True]
    assert [node.name for node in resolved] == ["uiuc-public-left", None, "ufl-public-right"]


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
    assert resolved[0].name is None
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
    client = NodeClient(ALICE, timeout_s=5.0, transport=node_api(_by_name))

    config = asyncio.run(client.get_config())

    assert config.node_name == "uiuc-public-left"
    assert config.follower_node_address is None


def test_the_client_raises_one_error_type_for_every_failure() -> None:
    client = NodeClient(DEAD, timeout_s=5.0, transport=node_api(_by_name))

    with pytest.raises(NodeApiError):
        asyncio.run(client.get_config())


def test_the_client_normalises_a_trailing_slash() -> None:
    """So paths can be appended without producing a double slash."""
    assert NodeClient(f"{ALICE}/", timeout_s=5.0).api_url == ALICE
