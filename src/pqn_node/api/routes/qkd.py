import asyncio
import logging
import random
import secrets
from typing import Annotated
from typing import Literal
from typing import NamedTuple
from typing import cast

import httpx
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from pydantic import BaseModel

from pqn_node.api.deps import ClientDep
from pqn_node.api.deps import StateDep
from pqn_node.api.hardware import find_rotator
from pqn_node.api.peers import measure_coincidences
from pqn_node.api.peers import require_follower_address
from pqn_node.api.routes.coordination import reset_coordination_state
from pqn_node.constants import BasisBool
from pqn_node.constants import CoincidenceLevel
from pqn_node.constants import OperationMode
from pqn_node.constants import QKDAngleValuesHWP
from pqn_node.constants import QKDEncodingBasis
from pqn_node.core.config import NodeRole
from pqn_node.core.config import NodeState
from pqn_node.core.config import protocol_cancelled_event
from pqn_node.core.config import qkd_result_received_event
from pqn_node.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/qkd", tags=["qkd"])


class QKDResult(BaseModel):
    n_matching_bits: int
    n_total_bits: int
    emoji: str
    role: str


class QKDRun(NamedTuple):
    """What one run of the QKD loop produced.

    ``bits`` is what the loop has always returned: in correlation mode each question's
    agreement (1 agreed, 0 disagreed), in entanglement mode the key bits on the questions
    whose two Answers matched. The rates and Coincidence levels are each question's own,
    correlation mode only; entanglement mode leaves them empty. ``inferred_bits`` is
    entanglement mode's bit for every question, in question order, which correlation mode
    and the GUI game leave empty.
    """

    bits: list[int]
    rates: list[float]
    levels: list[CoincidenceLevel]
    inferred_bits: list[int]


class UnattendedQKDAnswers(BaseModel):
    """Both players' Answers to one unattended SSM game, as Whobot sends them.

    The Answers use the same 'a' / 'b' strings the GUI uses: 'a' is left, 'b' is right.
    """

    leader_answers: list[str]
    follower_answers: list[str]


class UnattendedEntanglementResult(BaseModel):
    """An unattended SSM game's result in entanglement mode (decisions 11, 13).

    Both lists are the key bits, one entry per question, in question order: the bits the
    leader inferred from the coincidences, and the bits the follower actually recorded.
    Whobot knows the Answers it sent, so matching the two sides' bases is its job.
    """

    mode: Literal[OperationMode.ENTANGLEMENT] = OperationMode.ENTANGLEMENT
    leader_bits: list[int]
    follower_bits: list[int]


class UnattendedCorrelationResult(BaseModel):
    """An unattended SSM game's result in correlation mode (decisions 11, 13).

    One rate and one Coincidence level per question, in question order.
    """

    mode: Literal[OperationMode.CORRELATION] = OperationMode.CORRELATION
    rates: list[float]
    levels: list[CoincidenceLevel]


def _answers_to_basis_list(answers: list[str]) -> list[QKDEncodingBasis]:
    """Convert Answers as a player gives them ('a' left, 'b' right) to their bases, refusing any other string."""
    qkd_basis_list = []
    for basis_str in answers:
        if basis_str.lower() == "a":
            qkd_basis_list.append(QKDEncodingBasis.HV)
        elif basis_str.lower() == "b":
            qkd_basis_list.append(QKDEncodingBasis.DA)
        else:
            logger.exception("Invalid basis string: %s. Expected 'a' or 'b'", basis_str)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid basis string: {basis_str}. Expected 'a' or 'b'",
            )
    return qkd_basis_list


def _answer_polarization(answer: QKDEncodingBasis) -> QKDAngleValuesHWP:
    """Return the polarization one player's own Answer moves their waveplate to in correlation mode.

    Decision 1: left (HV) → V, right (DA) → D, with no random bit. The Calibrated angle
    applies where the move is made.
    """
    return QKDAngleValuesHWP.V if answer is QKDEncodingBasis.HV else QKDAngleValuesHWP.D


def _coincidence_level(rate: float) -> CoincidenceLevel:
    """Classify a coincidence rate by the nearest Reference rate (decision 7)."""
    references = settings.correlation_settings.reference_rates
    nearest = {
        CoincidenceLevel.HIGH: references.vv,
        CoincidenceLevel.MEDIUM: references.vd,
        CoincidenceLevel.LOW: references.dd,
    }
    return min(nearest, key=lambda level: abs(nearest[level] - rate))


async def _qkd(
    http_client: ClientDep,
    state: StateDep,
    timetagger_address: str,
    follower_node_address: str | None = None,
    integration_time_s: float | None = None,
) -> QKDRun:
    if follower_node_address is None:
        follower_node_address = require_follower_address()
    logger.debug("Starting QKD")
    hwp = find_rotator(settings.qkd_settings.hwp)
    # Every question is measured with this config; correlation mode also turns each count
    # into a rate with its integration time. An unattended game may integrate for another
    # time than the config's, so the time actually measured with is the one that divides.
    cfg = settings.chsh_settings.measurement_config
    integration_time = cfg.integration_time_s if integration_time_s is None else integration_time_s

    counts = []
    for basis in state.qkd_leader_basis_list:
        r = await http_client.post(f"http://{follower_node_address}/qkd/single_bit")

        if r.status_code != status.HTTP_200_OK:
            logger.error("Failed to handshake with follower: %s", r.text)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to handshake with follower",
            )
        logger.debug("Handshake with follower successful")

        if settings.operation_mode is OperationMode.CORRELATION:
            # Decision 1: in correlation mode the Answer alone moves this Node's own
            # waveplate (left → V, right → D); there is no random bit.
            hwp_angle = settings.polarization_angles.waveplate_angle(_answer_polarization(basis))
        else:
            int_choice = secrets.randbits(1)  # FIXME: Make this real quantum random.
            logger.debug("Chosen integer choice: %s", int_choice)
            state.qkd_bit_list.append(int_choice)
            # A move to a named polarization goes to its Calibrated angle, which is still a waveplate angle.
            hwp_angle = settings.polarization_angles.waveplate_angle(basis.angles[int_choice])
        hwp.move_to(hwp_angle)
        logger.debug("Moving half waveplate to angle: %s", hwp_angle)

        c = await measure_coincidences(http_client, timetagger_address, cfg, integration_time)
        counts.append(c)
        logger.debug("Counted %d coincidences", c)

    if settings.operation_mode is OperationMode.CORRELATION:
        # Decisions 1, 3 and 7: each count becomes a rate with the integration time it was
        # measured with, and its nearest Reference rate classifies it. The follower's
        # Answers are never fetched: a high or low Coincidence level means the two Answers
        # agreed, a medium one means they did not.
        rates = [count / integration_time for count in counts]
        levels = [_coincidence_level(rate) for rate in rates]
        agreement = [int(level is not CoincidenceLevel.MEDIUM) for level in levels]
        logger.info("Correlation-mode agreement: %s", agreement)
        return QKDRun(bits=agreement, rates=rates, levels=levels, inferred_bits=[])

    def get_outcome(state: int, basis: int, choice: int, counts: int) -> int:
        above = counts > settings.qkd_settings.discriminating_threshold
        return ((int(above) ^ choice) ^ (1 - state)) ^ basis

    outcome = []
    logger.debug(
        "Going for qkd_leader_basis_list: %s, qkd_bit_list: %s, counts: %s",
        state.qkd_leader_basis_list,
        state.qkd_bit_list,
        counts,
    )
    for basis, choice, count in zip(state.qkd_leader_basis_list, state.qkd_bit_list, counts, strict=False):
        out = get_outcome(settings.bell_state.value, BasisBool[basis.name].value, choice, count)
        logger.debug(
            "Calculating outcome for basis: %s, choice: %s, count: %s, outcome: %s", basis.name, choice, count, out
        )
        outcome.append(out)

    basis_list = [basis.name for basis in state.qkd_leader_basis_list]

    # FIXME: Send already binary basis instead of HV/AD.
    r = await http_client.post(f"http://{follower_node_address}/qkd/request_basis_list", json=basis_list)
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to request basis list from follower: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to request basis list from follower",
        )
    follower_basis_list = r.json()

    index_list = [i for i in range(len(follower_basis_list)) if follower_basis_list[i] == basis_list[i]]
    final_bits = [outcome[i] for i in index_list]

    logger.info("Final bits: %s", final_bits)

    # ``outcome`` is this Node's inferred bit for every question, not only the ones whose two
    # Answers matched: the unattended game reports it per question, one entry per Answer
    # (decision 13), and lets its caller pick the matched questions.
    return QKDRun(bits=final_bits, rates=[], levels=[], inferred_bits=outcome)


@router.post("")
async def qkd(
    http_client: ClientDep,
    state: StateDep,
    timetagger_address: str | None = None,
) -> list[int]:
    """Perform a QKD protocol with the given follower node."""
    if not state.qkd_leader_basis_list:
        logger.error("QKD basis list is empty")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QKD basis list is empty",
        )
    if not timetagger_address:
        # Was a request to "http://None/..." that failed as a 500; say what is actually missing.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="timetagger_address is required")

    return (await _qkd(http_client, state, timetagger_address)).bits


@router.post("/single_bit")
async def request_qkd_single_pass(state: StateDep) -> bool:
    hwp = find_rotator(settings.qkd_settings.request_hwp)

    # Check if we have basis choices available
    if state.qkd_single_bit_current_index >= len(state.qkd_follower_basis_list):
        logger.error("No more basis choices available in follower basis list")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No more basis choices available in follower basis list",
        )

    # Get the basis choice from the follower basis list
    basis_choice = state.qkd_follower_basis_list[state.qkd_single_bit_current_index]
    state.qkd_single_bit_current_index += 1

    if settings.operation_mode is OperationMode.CORRELATION:
        # Decision 1: in correlation mode the Answer alone moves this Node's own waveplate
        # (left → V, right → D), with no random bit. Decision 3: the leader never fetches
        # this Node's Answers, so none are recorded for it.
        hwp.move_to(settings.polarization_angles.waveplate_angle(_answer_polarization(basis_choice)))
        return True

    int_choice = secrets.randbits(1)  # FIXME: Make this real quantum random.

    state.qkd_request_basis_list.append(basis_choice)
    state.qkd_request_bit_list.append(int_choice)
    hwp_angle = settings.polarization_angles.waveplate_angle(basis_choice.angles[int_choice])

    hwp.move_to(hwp_angle)

    return True


@router.post("/request_basis_list")
def request_qkd_basis_list(leader_basis_list: list[str], state: StateDep) -> list[str]:
    """Return the list of basis angles for QKD."""
    # Check that lengths match
    if len(leader_basis_list) != len(state.qkd_request_basis_list):
        logger.error("Length of leader basis list does not match length of request basis list")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Length of leader basis list does not match length of request basis list",
        )

    ret = [basis.name for basis in state.qkd_request_basis_list]
    index_list = [i for i in range(len(leader_basis_list)) if ret[i] == leader_basis_list[i]]
    final_bits = [state.qkd_request_bit_list[i] for i in index_list]
    # This Node's own key bits, one per question, kept for the leader of an unattended game
    # to read back: it compares them with the bits it inferred, question by question
    # (decision 13). The list is cleared below, so it must be copied first.
    state.qkd_resulting_bit_list = list(state.qkd_request_bit_list)
    logger.error("Final bits: %s", final_bits)

    state.qkd_request_basis_list.clear()
    state.qkd_request_bit_list.clear()

    return ret


@router.post("/set_emoji")
def set_emoji(emoji: str, state: StateDep) -> None:
    """Set the emoji pick for QKD."""
    state.qkd_emoji_pick = emoji


@router.get("/question_order")
async def request_question_order(
    state: StateDep,
    http_client: ClientDep,
) -> list[int]:
    """
    Return the question order for QKD.

    If this node is a leader, it generates a random question order and stores it in the state.
    If this node is a follower, it requests the question order from the leader node.
    Returns the question order as a list of integers.
    """
    if state.role not in (NodeRole.LEADER, NodeRole.FOLLOWER):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The node must be a leader or follower.",
        )

    # Return cached question order if already generated
    if len(state.qkd_question_order) > 0:
        return state.qkd_question_order

    # Leader node: generate question order
    if state.role == NodeRole.LEADER:
        if state.followers_address == "":
            logger.error("Leader node has no follower address set")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Leader node has no follower address set",
            )

        question_range = range(
            settings.qkd_settings.minimum_question_index, settings.qkd_settings.maximum_question_index + 1
        )
        question_order = random.sample(
            list(question_range), settings.qkd_settings.bitstring_length
        )  # just choosing question order, no need for secure secrets package.
        state.qkd_question_order = question_order
        return state.qkd_question_order

    # Node is a follower
    if state.leaders_address == "":
        logger.error("Follower node has no leader address set")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Follower node has no leader address set",
        )
    r = await http_client.get(f"http://{state.leaders_address}/qkd/question_order")
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to get question order from leader: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get question order from leader",
        )
    state.qkd_question_order = r.json()
    return state.qkd_question_order


@router.get("/is_follower_ready")
async def is_follower_ready(state: StateDep) -> bool:
    """
    Check if the follower node is ready for QKD.

    Follower is ready when the state has the basis list with as many choices as the bitstring length.
    """
    if state.role != NodeRole.FOLLOWER:
        logger.error("Node is not a follower")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Node is not a follower",
        )

    return len(state.qkd_follower_basis_list) == settings.qkd_settings.bitstring_length


@router.post("/submit_result")
async def submit_result(result: QKDResult, state: StateDep) -> None:
    """QKD leader calls this endpoint of the follower to submit the QKD result as well as the emoji chosen."""
    state.qkd_emoji_pick = result.emoji
    state.qkd_n_matching_bits = result.n_matching_bits
    qkd_result_received_event.set()  # Signal that the result has been received
    logger.info("Received QKD result from follower: %s", result)


async def _wait_for_follower_ready(state: NodeState, http_client: httpx.AsyncClient) -> None:
    """Poll the follower until it's ready, checking every 0.5 seconds."""
    ready = False
    first_503 = True
    while not ready:
        # Check if protocol was cancelled
        if protocol_cancelled_event.is_set():
            logger.warning("Protocol cancelled while waiting for follower ready")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Protocol cancelled by peer or user")

        r = await http_client.get(f"http://{state.followers_address}/qkd/is_follower_ready")

        # If the follower disconnects while the leader is waiting, the 503 error of `Node is not a follower` error might come before we can handle the cancellation event.
        if r.status_code == status.HTTP_503_SERVICE_UNAVAILABLE and first_503:
            logger.warning("Received QKD result from follower: %s", r)
            first_503 = False
            continue

        if r.status_code != status.HTTP_200_OK:
            logger.error("Failed to check if follower is ready: %s", r.text)
            raise HTTPException(
                status_code=r.status_code,
                detail=f"Failed to check if follower is ready, {r.text}",
            )

        ready = r.json()
        if not ready:
            logger.info("Follower is not ready yet, waiting.")
            await asyncio.sleep(0.4)  # Make sure this is smaller than the protocol_cancelled event clear timer.

    logger.info("Follower is ready")


async def _submit_result_to_follower(state: NodeState, http_client: httpx.AsyncClient, qkd_result: QKDResult) -> None:
    """Submit the QKD result to the follower node."""
    r = await http_client.post(f"http://{state.followers_address}/qkd/submit_result", json=qkd_result.model_dump())
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to submit QKD result to follower: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit QKD result to follower",
        )
    logger.info("Successfully submitted QKD result to follower")


async def _submit_basis_list_leader(
    state: NodeState, http_client: httpx.AsyncClient, basis_list: list[QKDEncodingBasis], timetagger_address: str
) -> QKDResult:
    state.qkd_leader_basis_list = basis_list
    await _wait_for_follower_ready(state, http_client)

    run = await _qkd(http_client, state, timetagger_address, follower_node_address=state.followers_address)
    logger.info("Final QKD bits: %s", str(run.bits))

    # Decision 17: QKDResult keeps its shape. In correlation mode the bits are one per
    # question, 1 where the Coincidence level says the two Answers agreed, so the matching
    # count is their sum; in entanglement mode it is the length, one bit per matching Answer.
    n_matching_bits = sum(run.bits) if settings.operation_mode is OperationMode.CORRELATION else len(run.bits)

    # Assemble QKDResult object
    qkd_result = QKDResult(
        n_matching_bits=n_matching_bits,
        n_total_bits=settings.qkd_settings.bitstring_length,
        emoji=state.qkd_emoji_pick,
        role="leader",
    )

    # Submit result to follower
    await _submit_result_to_follower(state, http_client, qkd_result)
    return qkd_result


async def _submit_basis_list_follower(state: NodeState, basis_list: list[QKDEncodingBasis]) -> QKDResult:
    state.qkd_follower_basis_list = basis_list

    # don't wait for the event if the result is already set. This avoids deadlocks in case the result was set before this function is called.
    if state.qkd_n_matching_bits == -1:
        # Wait for EITHER result OR cancellation
        _done, pending = await asyncio.wait(
            [
                asyncio.create_task(qkd_result_received_event.wait()),
                asyncio.create_task(protocol_cancelled_event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()

        # Check if protocol was cancelled
        if protocol_cancelled_event.is_set():
            logger.warning("Protocol cancelled while waiting for QKD result")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Protocol cancelled by peer or user")

    # Reassemble the QKDResult object from the state
    qkd_result = QKDResult(
        n_matching_bits=state.qkd_n_matching_bits,
        n_total_bits=settings.qkd_settings.bitstring_length,
        emoji=state.qkd_emoji_pick,
        role="follower",
    )

    # Clear the event for the next QKD run
    qkd_result_received_event.clear()

    logger.info("Follower received QKD result: %s", state.qkd_n_matching_bits)
    return qkd_result


@router.post("/submit_selection_and_start")
async def submit_qkd_selection_and_start_qkd(
    state: StateDep, http_client: ClientDep, basis_list: list[str], timetagger_address: str = ""
) -> QKDResult:
    """
    GUI calls this function to submit the QKD basis selection and start the QKD protocol.

    This call is called by both leader and follower, depending on the node role, different actions are taken.
    """
    if state.role == NodeRole.INDEPENDENT:
        logger.error("Node must be either leader or follower to start QKD")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Node must be either leader or follower to start QKD",
        )

    # Convert 'a' or 'b' strings to QKDEncodingBasis enum values
    qkd_basis_list = _answers_to_basis_list(basis_list)

    if state.role == NodeRole.LEADER:
        if timetagger_address == "":
            logger.error("Leader must provide timetagger address to start QKD")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Leader must provide timetagger address to start QKD",
            )
        return await _submit_basis_list_leader(state, http_client, qkd_basis_list, timetagger_address)

    # If the node is not leading, it is assumed it is a follower due to previous check
    return await _submit_basis_list_follower(state, qkd_basis_list)


async def _reset_after_unattended(http_client: httpx.AsyncClient, state: NodeState, follower_node_address: str) -> None:
    """Reset coordination on both Nodes after an unattended game, whether it completed or not (decision 11).

    The follower's reset is best-effort: a follower that cannot be reached cannot be reset
    by trying harder, and this Node's own reset must happen either way.
    """
    try:
        r = await http_client.post(f"http://{follower_node_address}/coordination/reset_coordination_state")
        if r.status_code != status.HTTP_200_OK:
            logger.error("Failed to reset coordination on the follower: %s", r.text)
    except httpx.HTTPError as e:
        logger.warning("Could not reset coordination on the follower: %s. Proceeding with our own reset.", str(e))
    await reset_coordination_state(state, http_client)


@router.post("/unattended/follower")
async def unattended_follower(
    request: Request, leaders_name: str, leaders_port: int, basis_list: list[str], state: StateDep
) -> bool:
    """Take this Node into the follower role for an unattended SSM game, without asking anyone (decision 11).

    The leader of an unattended game calls this with this Node's own Answers, whatever
    either Node was doing: a game already in progress is taken over, and the leader resets
    coordination on both Nodes once the game is done.
    """
    if request.client is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request lacks the client's host")
    qkd_basis_list = _answers_to_basis_list(basis_list)
    state.role = NodeRole.FOLLOWER
    state.leaders_name = leaders_name
    state.leaders_address = f"{request.client.host}:{leaders_port}"
    state.qkd_follower_basis_list = qkd_basis_list
    # A game taken over starts from its first question and records nothing from the one it replaced.
    state.qkd_single_bit_current_index = 0
    state.qkd_request_basis_list = []
    state.qkd_request_bit_list = []
    state.qkd_resulting_bit_list = []
    return True


@router.get("/unattended/follower_bits")
def unattended_follower_bits(state: StateDep) -> list[int]:
    """Return this Node's actual key bits from its last entanglement-mode game, for the leader of an unattended game.

    One bit per question, in question order: the actual bit this Node recorded for each
    question, which the leader compares with the bits it inferred (decision 13). The leader
    knows the Answers it sent, so picking the questions whose bases matched is its job.
    """
    return state.qkd_resulting_bit_list


async def _fetch_follower_bits(http_client: httpx.AsyncClient, follower_node_address: str) -> list[int]:
    """Fetch the follower's actual key bits, one per question, for the entanglement-mode result.

    Decision 13: the unattended game's result puts the bits this Node inferred from the
    coincidences next to the bits the follower actually recorded.
    """
    r = await http_client.get(f"http://{follower_node_address}/qkd/unattended/follower_bits")
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to fetch the follower's bits: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch the follower's bits",
        )
    return cast("list[int]", r.json())


@router.post("/unattended")
async def unattended_qkd(  # noqa: PLR0913 - the game takes both players' Answers and the addresses it drives
    answers: UnattendedQKDAnswers,
    request: Request,
    state: StateDep,
    http_client: ClientDep,
    timetagger_address: str,
    integration_time_s: Annotated[float | None, Query(gt=0)] = None,
) -> UnattendedEntanglementResult | UnattendedCorrelationResult:
    """Play one unattended SSM game, whatever either Node is doing (decisions 11, 13).

    Whobot sends both players' Answers here; this Node puts its configured follower in the
    follower role without asking anyone, plays the same loop the GUI game plays, resets
    coordination on both Nodes and returns the result: in entanglement mode both sides' key
    bits, one entry per question, in correlation mode each question's rate and Coincidence
    level. The game takes any number of questions, independent of ``bitstring_length`` and
    the GUI's question order.
    """
    if not answers.leader_answers:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Answers are empty")
    if len(answers.leader_answers) != len(answers.follower_answers):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The two players' Answers must be the same length",
        )
    leader_basis_list = _answers_to_basis_list(answers.leader_answers)
    # Both players' Answers are validated before either Node is touched: a bad string
    # anywhere is a 400 that changes nothing.
    _answers_to_basis_list(answers.follower_answers)
    follower_node_address = require_follower_address()

    # Decision 11: the follower is put in the follower role with its own Answers, without
    # asking anyone. That call answering is what makes it ready: no polling, and no
    # bitstring_length for the number of questions to match.
    r = await http_client.post(
        f"http://{follower_node_address}/qkd/unattended/follower",
        params={"leaders_name": settings.node_name, "leaders_port": request.scope["server"][1]},
        json=answers.follower_answers,
    )
    if r.status_code != status.HTTP_200_OK:
        logger.error("Failed to set up the follower for the unattended game: %s", r.text)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set up the follower for the unattended game",
        )

    # This Node leads the game it was asked to run, whatever it was doing before. Bits left
    # over from an earlier game would misalign this one's: the loop zips them with its bases.
    state.role = NodeRole.LEADER
    state.followers_address = follower_node_address
    state.qkd_leader_basis_list = leader_basis_list
    state.qkd_bit_list = []

    try:
        run = await _qkd(
            http_client,
            state,
            timetagger_address,
            follower_node_address=follower_node_address,
            integration_time_s=integration_time_s,
        )
        follower_bits: list[int] = []
        if settings.operation_mode is OperationMode.ENTANGLEMENT:
            follower_bits = await _fetch_follower_bits(http_client, follower_node_address)
    except Exception:
        # Decision 11: coordination is reset on both Nodes whether the game completed or not.
        await _reset_after_unattended(http_client, state, follower_node_address)
        raise

    await _reset_after_unattended(http_client, state, follower_node_address)

    if settings.operation_mode is OperationMode.CORRELATION:
        return UnattendedCorrelationResult(rates=run.rates, levels=run.levels)
    return UnattendedEntanglementResult(leader_bits=run.inferred_bits, follower_bits=follower_bits)
