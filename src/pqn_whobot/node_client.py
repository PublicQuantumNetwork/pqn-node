"""Whobot's client for the Node API, the only way it talks to a Node.

Every call carries a timeout, and every failure — refused connection, HTTP error,
unparseable body — arrives as a ``NodeApiError``.
"""

import logging

import httpx
from pydantic import BaseModel
from pydantic import ValidationError

logger = logging.getLogger(__name__)


_NODE_CONFIG_KEYS = {"node_name", "follower_node_address"}


class NodeApiError(Exception):
    """A call to a Node failed. The message is what an operator sees."""


class NodeConfigResponse(BaseModel):
    """The part of ``GET /node/config`` Whobot reads.

    ``node_name`` is optional because Nodes running code from before it was added to the
    endpoint answer without it. Such a Node is out of date, not unreachable.
    """

    node_name: str | None = None
    follower_node_address: str | None = None


class NodeClient:
    """Talks to one Node, applying ``timeout_s`` to every call.

    Each call opens and closes its own connection; Nodes are probed minutes apart at most,
    so there is nothing for a pooled connection to save.
    """

    def __init__(self, api_url: str, timeout_s: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        return f"NodeClient({self.api_url!r}, timeout_s={self.timeout_s})"

    async def _get_json(self, path: str) -> object:
        url = f"{self.api_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            msg = f"{type(e).__name__}: {e}"
            logger.warning("GET %s failed: %s", url, msg)
            raise NodeApiError(msg) from e
        except ValueError as e:  # a 200 that isn't JSON: something other than a Node answered
            msg = f"{url} did not return JSON: {e}"
            logger.warning(msg)
            raise NodeApiError(msg) from e

    async def get_config(self) -> NodeConfigResponse:
        """Ask the Node for its name and follower address."""
        payload = await self._get_json("/node/config")
        # Both fields are optional, so a bare `{}` would validate: check that the response
        # carries at least one of them, or anything serving JSON on that port passes for a Node.
        if not isinstance(payload, dict) or not _NODE_CONFIG_KEYS & payload.keys():
            msg = f"{self.api_url}/node/config is not a Node's config: {str(payload)[:100]}"
            raise NodeApiError(msg)
        try:
            return NodeConfigResponse.model_validate(payload)
        except ValidationError as e:
            msg = f"unexpected /node/config response: {e}"
            raise NodeApiError(msg) from e
