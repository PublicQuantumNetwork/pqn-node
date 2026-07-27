"""The Node Registry: the Nodes listed in ``whobot.toml``, with their names and reachability."""

import asyncio
import logging
import time
from dataclasses import dataclass

from pqn_whobot.config import WhobotSettings
from pqn_whobot.node_client import NodeApiError
from pqn_whobot.node_client import NodeClient

logger = logging.getLogger(__name__)


_NO_NAME_WARNING = "no node_name in /node/config; the Node is running older code — update it"


@dataclass(frozen=True)
class Node:
    """One registered Node as Whobot currently sees it.

    ``name`` is None when the Node did not answer, or answered without one. ``warning``
    describes a Node that answered but not with what Whobot expects — reachable, but not
    fully usable.
    """

    api_url: str
    name: str | None
    reachable: bool
    error: str | None = None
    warning: str | None = None
    latency_ms: float | None = None


async def resolve_node(client: NodeClient) -> Node:
    """Ask one Node for its name, timing the call. Unreachable is a result, not an exception."""
    started = time.perf_counter()
    try:
        config = await client.get_config()
    except NodeApiError as e:
        return Node(api_url=client.api_url, name=None, reachable=False, error=str(e))
    return Node(
        api_url=client.api_url,
        name=config.node_name,
        reachable=True,
        warning=None if config.node_name else _NO_NAME_WARNING,
        latency_ms=(time.perf_counter() - started) * 1000,
    )


async def resolve_nodes(settings: WhobotSettings) -> list[Node]:
    """Resolve every registered Node, concurrently, returning them in registry order.

    Concurrent because ``/node/config`` touches no hardware, so there is nothing for two
    Nodes to contend for — unlike the digest, which runs Games and so must be serial.
    """
    clients = [NodeClient(entry.api_url, settings.reachability_timeout_s) for entry in settings.nodes]
    return list(await asyncio.gather(*(resolve_node(client) for client in clients)))
