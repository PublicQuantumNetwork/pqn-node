"""Operations on a Node's host, and read-only dumps of what the process believes.

Both host operations need per-Node provisioning (see the README): screenshot needs
``maim`` installed, and reboot needs passwordless ``systemctl reboot``. Capture works
only because KDE autostart launches the API from inside the Plasma session, so it
inherits ``DISPLAY`` and ``XAUTHORITY`` — nothing here reconstructs that environment.
"""

import asyncio
import logging

from fastapi import APIRouter
from fastapi import BackgroundTasks
from fastapi import HTTPException
from fastapi import Response
from fastapi import status
from pydantic import BaseModel

from pqn_node.api.deps import StateDep
from pqn_node.core.config import NodeState
from pqn_node.core.config import Settings
from pqn_node.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system")


class RebootAck(BaseModel):
    scheduled: bool
    detail: str


async def _run(command: tuple[str, ...], timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run ``command``, returning (returncode, stdout, stderr). Raises on timeout."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout, stderr


@router.get(
    "/screenshot",
    tags=["system"],
    response_class=Response,
    responses={200: {"content": {"image/png": {}}, "description": "PNG of the Node's display"}},
)
async def screenshot(timeout_s: float = 20.0) -> Response:
    """Capture the Node's display as a PNG."""
    command = ("maim", "--hidecursor")
    try:
        returncode, image, errors = await _run(command, timeout_s)
    except FileNotFoundError:
        detail = f"'{command[0]}' is not installed on this Node"
        logger.exception(detail)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail) from None
    except TimeoutError:
        detail = f"screenshot timed out after {timeout_s:.0f}s"
        logger.error(detail)  # noqa: TRY400 - the traceback adds nothing to a timeout
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, detail) from None

    if returncode != 0 or not image:
        detail = f"screenshot failed: {errors.decode(errors='replace').strip() or f'exit code {returncode}'}"
        logger.error(detail)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)

    return Response(content=image, media_type="image/png")


async def _reboot_after_response(delay_s: float) -> None:
    command = ("sudo", "systemctl", "reboot")
    await asyncio.sleep(delay_s)
    logger.warning("Rebooting: %s", " ".join(command))
    try:
        returncode, _, errors = await _run(command, timeout_s=30.0)
    except (OSError, TimeoutError):
        logger.exception("Reboot command failed to run")
        return
    if returncode != 0:
        logger.error("Reboot command exited %d: %s", returncode, errors.decode(errors="replace").strip())


@router.post("/reboot", tags=["system"])
async def reboot(background_tasks: BackgroundTasks, delay_s: float = 1.0) -> RebootAck:
    """Reboot the Node's host.

    Scheduled as a background task so the caller gets a response instead of a dropped
    connection, and can poll until the API answers again. ``delay_s`` is how long the
    response has to leave the machine before systemd starts tearing it down.
    """
    background_tasks.add_task(_reboot_after_response, delay_s)
    return RebootAck(scheduled=True, detail=f"Rebooting in {delay_s:.0f}s")


@router.get("/state", tags=["debug"])
async def get_node_state(state: StateDep) -> NodeState:
    """Dump the Node's live coordination and protocol state, for eyeballing a running Node."""
    return state


@router.get("/settings", tags=["debug"])
async def get_node_settings() -> Settings:
    """Dump the settings the Node is actually running with, including any applied at runtime."""
    return settings
