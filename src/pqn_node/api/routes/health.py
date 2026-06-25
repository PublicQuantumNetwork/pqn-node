import concurrent.futures
import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
import serial
from fastapi import APIRouter
from pqn_hardware.network.client import Client
from pydantic import BaseModel
from pydantic import Field

from pqn_node.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

_ROUTER_TIMEOUT_MS = 5000
_ROUTER_WALL_TIMEOUT_S = 6.0

_DEVICES_WALL_TIMEOUT_S = 6.0

_SERIAL_WALL_TIMEOUT_S = 3.0

_FOLLOWER_TIMEOUT_S = 5.0
_FOLLOWER_WALL_TIMEOUT_S = 6.0

_T = TypeVar("_T")
_probe_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-probe")


def _run_with_timeout(fn: Callable[[], _T], timeout_s: float) -> _T:
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
            follower_node = _run_with_timeout(
                lambda: _probe_follower(follower_node_address), _FOLLOWER_WALL_TIMEOUT_S
            )
        except concurrent.futures.TimeoutError:
            follower_node = ComponentStatus(reachable=False, error="timeout")
    else:
        follower_node = None

    _apply_games_override(router_status, follower_node)

    return HealthStatus(
        router=router_status,
        devices=devices,
        rotary_encoder=rotary_encoder,
        follower_node=follower_node,
    )


def _apply_games_override(router_status: ComponentStatus, follower_node: ComponentStatus | None) -> None:
    ga = settings.games_availability
    if not router_status.reachable:
        ga.chsh = False
        ga.qf = False
        ga.ssm = False
    elif follower_node is not None and not follower_node.reachable:
        ga.chsh = False
        ga.ssm = False
