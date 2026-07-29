"""Whobot's client for the Node API, the only way it talks to a Node.

Every call carries a timeout, and every failure — refused connection, HTTP error,
unparseable body — arrives as a ``NodeApiError``.
"""

import logging

import httpx
from pydantic import BaseModel
from pydantic import ValidationError

from pqn_node.core.config import GamesAvailability

logger = logging.getLogger(__name__)


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

    async def _send_json(self, method: str, path: str, body: object | None = None) -> object:
        url = f"{self.api_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
                response = await client.request(method, url, json=body)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as e:
            msg = f"{type(e).__name__}: {e}"
            logger.warning("%s %s failed: %s", method, url, msg)
            raise NodeApiError(msg) from e
        except ValueError as e:  # a 200 that isn't JSON: something other than a Node answered
            msg = f"{url} did not return JSON: {e}"
            logger.warning(msg)
            raise NodeApiError(msg) from e

    def _parse_availability(self, payload: object) -> GamesAvailability:
        # Every Game must be named: the model defaults each to True, so a partial answer
        # would report a Game as available on a Node that never mentioned it.
        if not isinstance(payload, dict) or not payload.keys() >= GamesAvailability.model_fields.keys():
            msg = f"{self.api_url}/games/availability is not a Games availability: {str(payload)[:100]}"
            raise NodeApiError(msg)
        try:
            return GamesAvailability.model_validate(payload)
        except ValidationError as e:
            msg = f"{self.api_url} did not answer with a Games availability: {e}"
            raise NodeApiError(msg) from e

    async def get_availability(self) -> GamesAvailability:
        """Ask the Node which Games it currently offers.

        This is the Node's *effective* availability — its configuration gated by the last
        hardware probe — because that is what the endpoint returns. A Game switched on in
        config still reads as unavailable while the router it needs is unreachable.
        """
        return self._parse_availability(await self._send_json("GET", "/games/availability"))

    async def set_availability(self, availability: GamesAvailability) -> GamesAvailability:
        """Set which Games the Node offers, persistently and without a restart.

        Returns what the Node reports *afterwards*, which is not necessarily what was
        asked for: the endpoint answers with effective availability, so a Game switched on
        here still reads as unavailable if its hardware is unreachable.
        """
        payload = await self._send_json("PUT", "/games/availability", availability.model_dump())
        return self._parse_availability(payload)

    async def get_config(self) -> NodeConfigResponse:
        """Ask the Node for its name and follower address."""
        payload = await self._send_json("GET", "/node/config")
        # *Any* of the fields will do here, unlike availability: a Node running code from
        # before node_name existed answers with only the follower address, and that is out
        # of date rather than unreachable. Both fields are optional, so without this check
        # a bare `{}` would validate and anything on that port would pass for a Node.
        if not isinstance(payload, dict) or not NodeConfigResponse.model_fields.keys() & payload.keys():
            msg = f"{self.api_url}/node/config is not a Node's config: {str(payload)[:100]}"
            raise NodeApiError(msg)
        try:
            return NodeConfigResponse.model_validate(payload)
        except ValidationError as e:
            msg = f"unexpected /node/config response: {e}"
            raise NodeApiError(msg) from e
