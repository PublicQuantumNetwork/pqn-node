import concurrent.futures
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

import httpx
import serial
from fastapi import APIRouter
from pqn_hardware.network.client import Client
from pydantic import BaseModel
from pydantic import Field

from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

_ROUTER_TIMEOUT_MS = 5000
_ROUTER_WALL_TIMEOUT_S = 6.0

_DEVICES_WALL_TIMEOUT_S = 6.0

_SERIAL_WALL_TIMEOUT_S = 3.0

_FOLLOWER_TIMEOUT_S = 5.0
_FOLLOWER_WALL_TIMEOUT_S = 6.0

_probe_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")


# FIXME: Why does this need to be its own function like this?
def _run_with_timeout[T](fn: Callable[[], T], timeout_s: float) -> T:
    return _probe_executor.submit(fn).result(timeout=timeout_s)


class ComponentStatus(BaseModel):
    reachable: bool
    error: str | None = None
    latency_ms: float | None = None


class DeviceStatus(ComponentStatus):
    provider: str
    name: str
    purpose: str  # human-readable label describing what the device is used for


class HealthStatus(BaseModel):
    router: ComponentStatus
    devices: list[DeviceStatus] = Field(default_factory=list)
    rotary_encoder: ComponentStatus | None = None
    follower_node: ComponentStatus | None = None

    @property
    def all_ok(self) -> bool:
        if not self.router.reachable:
            return False
        if any(not d.reachable for d in self.devices):
            return False
        if self.rotary_encoder is not None and not self.rotary_encoder.reachable:
            return False
        return not (self.follower_node is not None and not self.follower_node.reachable)


@dataclass
class _LastProbe:
    """The most recent probe's *inputs* to the availability gate — not its result.

    Holding the inputs is what lets `/games/availability` state, at any moment:

        availability == effective_availability(config.toml, most recent probe)

    Availability is recomputed from the pristine config on every read rather than
    read back from a stored answer. Two things follow, and both are load-bearing:

    - a gated-off flag can never become an input to the next gating, so it cannot
      latch games off; they come back on their own once hardware is reachable;
    - a config change (`PUT /games/availability`) is visible immediately, with no
      restart and no wait for the next probe.

    `ran` is False until the first probe completes; see `get_effective_availability`.
    """

    ran: bool = False
    router: ComponentStatus = field(default_factory=lambda: ComponentStatus(reachable=False, error="no probe yet"))
    follower_node: ComponentStatus | None = None


_last_probe = _LastProbe()


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _format_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _connect_router() -> tuple[ComponentStatus, Client | None]:
    start = time.perf_counter()
    try:
        client = Client(
            host=settings.router_address,
            port=settings.router_port,
            router_name=settings.router_name,
            timeout=_ROUTER_TIMEOUT_MS,
        )
    except Exception as e:  # noqa: BLE001 - any failure to connect must be reported, not swallowed
        return ComponentStatus(reachable=False, error=_format_error(e)), None
    return ComponentStatus(reachable=True, latency_ms=_elapsed_ms(start)), client


def _configured_devices() -> list[tuple[str, str, str]]:
    """Return deduplicated (provider, name, purpose) triples for all configured devices.

    HWP fields default to ("", "") when unconfigured; those are filtered out.
    Timetagger is optional (None means unused). When two settings share the same
    (provider, name) pair their purposes are merged — e.g. "CHSH HWP / QKD HWP" —
    so each physical device appears exactly once in the health report.
    """
    labeled: list[tuple[str, str, str]] = [
        (*settings.chsh_settings.hwp, "CHSH leader HWP"),
        (*settings.chsh_settings.request_hwp, "CHSH follower HWP"),
        (*settings.qkd_settings.hwp, "QKD leader HWP"),
        (*settings.qkd_settings.request_hwp, "QKD follower HWP"),
        *([(settings.timetagger[0], settings.timetagger[1], "Timetagger")] if settings.timetagger else []),
    ]
    # Preserve insertion order while merging purposes for duplicate (provider, name) pairs.
    merged: dict[tuple[str, str], list[str]] = {}
    for provider, name, purpose in labeled:
        if not provider or not name:
            continue
        key = (provider, name)
        merged.setdefault(key, []).append(purpose)
    return [(provider, name, " / ".join(purposes)) for (provider, name), purposes in merged.items()]


def _probe_devices(client: Client) -> list[DeviceStatus]:
    configured = _configured_devices()
    # Group by provider so we make one get_available_devices call per provider.
    by_provider: dict[str, list[tuple[str, str]]] = {}
    for provider, name, purpose in configured:
        by_provider.setdefault(provider, []).append((name, purpose))

    results: list[DeviceStatus] = []
    for provider, name_purpose_pairs in by_provider.items():
        start = time.perf_counter()
        try:
            available = client.get_available_devices(provider)
        except Exception as e:  # noqa: BLE001 - any failure must surface as device status, not a crash
            err = _format_error(e)
            results.extend(
                DeviceStatus(provider=provider, name=name, purpose=purpose, reachable=False, error=err)
                for name, purpose in name_purpose_pairs
            )
            continue
        latency = _elapsed_ms(start)
        for name, purpose in name_purpose_pairs:
            if name in available:
                results.append(
                    DeviceStatus(provider=provider, name=name, purpose=purpose, reachable=True, latency_ms=latency)
                )
            else:
                results.append(
                    DeviceStatus(
                        provider=provider,
                        name=name,
                        purpose=purpose,
                        reachable=False,
                        error=f"device '{name}' not registered on provider '{provider}'",
                    )
                )
    return results


def _probe_rotary_encoder() -> ComponentStatus | None:
    if settings.virtual_rotator:
        return None
    start = time.perf_counter()
    try:
        with serial.Serial(settings.rotary_encoder_address, 115200, timeout=1):
            pass
    except Exception as e:  # noqa: BLE001 - any failure must surface, not crash the endpoint
        return ComponentStatus(reachable=False, error=_format_error(e))
    return ComponentStatus(reachable=True, latency_ms=_elapsed_ms(start))


def _probe_follower(follower_node_address: str) -> ComponentStatus:
    start = time.perf_counter()
    try:
        with httpx.Client(timeout=_FOLLOWER_TIMEOUT_S) as http:
            response = http.get(f"http://{follower_node_address}/")
        response.raise_for_status()
    except Exception as e:  # noqa: BLE001 - any failure must surface, not crash the endpoint
        return ComponentStatus(reachable=False, error=_format_error(e))
    return ComponentStatus(reachable=True, latency_ms=_elapsed_ms(start))


@router.get("/")
def health() -> HealthStatus:
    """Probe router, configured devices, rotary encoder, and optional follower node."""
    follower_node_address = settings.follower_node_address
    try:
        router_status, client = _run_with_timeout(_connect_router, _ROUTER_WALL_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        router_status = ComponentStatus(reachable=False, error="timeout")
        client = None

    if client is not None:
        try:
            devices = _run_with_timeout(lambda: _probe_devices(client), _DEVICES_WALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            devices = [
                DeviceStatus(provider=provider, name=name, purpose=purpose, reachable=False, error="timeout")
                for provider, name, purpose in _configured_devices()
            ]
        finally:
            client.disconnect()
    else:
        devices = [
            DeviceStatus(provider=provider, name=name, purpose=purpose, reachable=False, error="router unreachable")
            for provider, name, purpose in _configured_devices()
        ]

    if not settings.virtual_rotator:
        try:
            rotary_encoder = _run_with_timeout(_probe_rotary_encoder, _SERIAL_WALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            rotary_encoder = ComponentStatus(reachable=False, error="timeout")
    else:
        rotary_encoder = _probe_rotary_encoder()

    if follower_node_address:
        try:
            follower_node = _run_with_timeout(lambda: _probe_follower(follower_node_address), _FOLLOWER_WALL_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            follower_node = ComponentStatus(reachable=False, error="timeout")
    else:
        follower_node = None

    # Record the gate's inputs, not its result: /games/availability recomputes from
    # the pristine config on every read, so games recover once hardware comes back.
    _last_probe.router = router_status
    _last_probe.follower_node = follower_node
    _last_probe.ran = True

    return HealthStatus(
        router=router_status,
        devices=devices,
        rotary_encoder=rotary_encoder,
        follower_node=follower_node,
    )


def effective_availability(
    configured: GamesAvailability,
    router_status: ComponentStatus,
    follower_node: ComponentStatus | None,
) -> GamesAvailability:
    """Gate the configured game availability on live hardware reachability.

    Pure: same inputs always give the same answer, and neither `configured` nor
    any shared state is mutated. Callers own the returned copy.

    Two properties follow from starting at `configured` and only ever clearing
    flags, and both are load-bearing:

    - **Games recover on their own.** The result is re-derived from config each
      call rather than revised from the previous result, so once the hardware is
      reachable again the next probe returns True with no explicit re-enable
      step. There is nothing to reset by hand and no restart needed.
    - **config.toml is an absolute veto.** No branch here assigns True, so a game
      disabled in config can never be switched on by a healthy probe.

    `configured` must therefore be the pristine values parsed from config.toml —
    pass `settings.games_availability`, never a previously gated result. Passing
    a gated result back in reintroduces the latching bug this function replaced:
    the flags would ratchet toward all-off and stay there.
    """
    ga = configured.model_copy()
    if not router_status.reachable:
        ga.chsh = False
        ga.qf = False
        ga.ssm = False
    elif follower_node is not None and not follower_node.reachable:
        ga.chsh = False
        ga.ssm = False
    return ga


def get_effective_availability() -> GamesAvailability:
    """Return the current configuration, gated by the most recent health probe.

    Backs `GET /games/availability`. Read-only: this does not probe hardware, so
    the *hardware* half of the answer is only as fresh as the last `health()`
    call — app startup, the daily digest, or any manual hit on `/health/`, which
    is what re-enables games on a Node whose hardware has recovered. The *config*
    half is always current, so a `PUT /games/availability` shows up here at once.

    Falls back to the configured values when no probe has run yet, so the
    endpoint reports config rather than claiming everything is disabled.
    """
    if not _last_probe.ran:
        return settings.games_availability.model_copy()
    return effective_availability(settings.games_availability, _last_probe.router, _last_probe.follower_node)
