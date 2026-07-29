"""Whobot's client for the Node API, the only way it talks to a Node.

Every call carries a timeout, and every failure — refused connection, HTTP error,
unparseable body — arrives as a ``NodeApiError``.

``HealthStatus`` and ``ChshResult`` are imported from the route modules that produce them, so
there is one definition of each rather than a copy that can drift. The cost is that importing
this pulls in a FastAPI route module and everything it imports; extracting the shared response
models into a module of their own is a later refactor.
"""

import logging

import httpx
from pydantic import BaseModel
from pydantic import ValidationError

from pqn_node.api.routes.chsh import ChshResult
from pqn_node.api.routes.health import HealthStatus
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


class RebootAck(BaseModel):
    """What ``POST /system/reboot`` answers with: the reboot was *scheduled*, not done.

    The Node replies before it starts dying, so an ack says nothing about whether the
    machine comes back. Only polling it does.
    """

    scheduled: bool
    detail: str | None = None


def _detail(response: httpx.Response) -> str:
    """Pull FastAPI's ``detail`` out of an error body, falling back to the status line.

    Worth the few lines: the Node says *why* it refused — "'maim' is not installed on this
    Node" — and without this an operator sees only "Server error '503'", which sends them
    to the Node's logs to learn something the Node already told them.
    """
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if detail else response.reason_phrase


class NodeClient:
    """Talks to one Node. Every call states how long it may take.

    The bound belongs to the call rather than to the client, because the same Node answers
    "are you there?" in milliseconds and runs a CHSH for ten minutes. A client that fixed one
    timeout would need to be rebuilt to ask a different question of the same machine.

    Each call opens and closes its own connection; Nodes are probed minutes apart at most,
    so there is nothing for a pooled connection to save.
    """

    def __init__(self, api_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self._transport = transport

    def __repr__(self) -> str:
        return f"NodeClient({self.api_url!r})"

    async def _request(
        self,
        method: str,
        path: str,
        timeout_s: float,
        body: object | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Make one call, turning every way it can fail into a ``NodeApiError``."""
        url = f"{self.api_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout_s, transport=self._transport) as client:
                response = await client.request(method, url, json=body, params=params)
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as e:
            # The Node answered and refused, and it said why. That reason is the message.
            msg = f"HTTP {e.response.status_code}: {_detail(e.response)}"
            logger.warning("%s %s refused: %s", method, url, msg)
            raise NodeApiError(msg) from e
        except httpx.HTTPError as e:
            msg = f"{type(e).__name__}: {e}"
            logger.warning("%s %s failed: %s", method, url, msg)
            raise NodeApiError(msg) from e

    async def _send_json(
        self,
        method: str,
        path: str,
        timeout_s: float,
        body: object | None = None,
        params: dict[str, str] | None = None,
    ) -> object:
        response = await self._request(method, path, timeout_s, body, params)
        try:
            return response.json()
        except ValueError as e:  # a 200 that isn't JSON: something other than a Node answered
            msg = f"{self.api_url}{path} did not return JSON: {e}"
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

    async def get_availability(self, timeout_s: float) -> GamesAvailability:
        """Ask the Node which Games it currently offers.

        This is the Node's *effective* availability — its configuration gated by the last
        hardware probe — because that is what the endpoint returns. A Game switched on in
        config still reads as unavailable while the router it needs is unreachable.
        """
        return self._parse_availability(await self._send_json("GET", "/games/availability", timeout_s))

    async def set_availability(self, availability: GamesAvailability, timeout_s: float) -> GamesAvailability:
        """Set which Games the Node offers, persistently and without a restart.

        Returns what the Node reports *afterwards*, which is not necessarily what was
        asked for: the endpoint answers with effective availability, so a Game switched on
        here still reads as unavailable if its hardware is unreachable.
        """
        payload = await self._send_json("PUT", "/games/availability", timeout_s, availability.model_dump())
        return self._parse_availability(payload)

    async def get_screenshot(self, timeout_s: float) -> bytes:
        """Capture the Node's display, returning the image bytes as they arrived.

        The endpoint answers ``image/png``. The content type is checked because a proxy or
        a captive portal on the way in would otherwise be uploaded to Slack as a screenshot.
        """
        response = await self._request("GET", "/system/screenshot", timeout_s)
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/") or not response.content:
            msg = (
                f"{self.api_url}/system/screenshot did not return an image "
                f"({content_type or 'no content type'}, {len(response.content)} bytes)"
            )
            raise NodeApiError(msg)
        return response.content

    async def reboot(self, timeout_s: float) -> RebootAck:
        """Ask the Node to reboot, returning its acknowledgement.

        The Node schedules the reboot and answers before it dies, so this returns while the
        machine is still up. Whether it comes back is a separate question, answered by
        polling.
        """
        payload = await self._send_json("POST", "/system/reboot", timeout_s)
        if not isinstance(payload, dict) or "scheduled" not in payload:
            msg = f"{self.api_url}/system/reboot did not acknowledge the reboot: {str(payload)[:100]}"
            raise NodeApiError(msg)
        try:
            return RebootAck.model_validate(payload)
        except ValidationError as e:
            msg = f"unexpected /system/reboot response: {e}"
            raise NodeApiError(msg) from e

    def _validated[T: BaseModel](self, model: type[T], payload: object, what: str) -> T:
        """Parse a response into a model, naming what was being read when it doesn't fit."""
        try:
            return model.model_validate(payload)
        except ValidationError as e:
            msg = f"{self.api_url} did not answer with {what}: {e}"
            raise NodeApiError(msg) from e

    async def get_health(self, timeout_s: float) -> HealthStatus:
        """Probe the Node's hardware: its router, devices, rotary encoder and follower.

        The endpoint takes no parameters — it reads the follower's address from the Node's own
        settings — so there is nothing for a caller to get wrong here.
        """
        payload = await self._send_json("GET", "/health/", timeout_s)
        return self._validated(HealthStatus, payload, "a health status")

    async def run_chsh(self, timetagger_address: str, basis: tuple[float, float], timeout_s: float) -> ChshResult:
        """Run one CHSH measurement at these angles, and return what it measured.

        Minutes of hardware work, which is why the caller says how many.
        """
        params = {"timetagger_address": timetagger_address}
        payload = await self._send_json("POST", "/chsh/", timeout_s, list(basis), params)
        return self._validated(ChshResult, payload, "a CHSH result")

    async def run_fortune(self, timetagger_address: str, timeout_s: float) -> list[int]:
        """Run one Quantum Fortune, returning the number each channel drew.

        ``fortune_size`` and ``channels`` are deliberately not sent: the Node falls back to its
        own ``rng_settings``, and an unattended run must not override per-Node calibration.
        """
        payload = await self._send_json(
            "GET", "/rng/fortune", timeout_s, params={"timetagger_address": timetagger_address}
        )
        if not isinstance(payload, list) or not all(isinstance(drawn, int) for drawn in payload):
            msg = f"{self.api_url}/rng/fortune is not a fortune per channel: {str(payload)[:100]}"
            raise NodeApiError(msg)
        return payload

    async def get_config(self, timeout_s: float) -> NodeConfigResponse:
        """Ask the Node for its name and follower address."""
        payload = await self._send_json("GET", "/node/config", timeout_s)
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
