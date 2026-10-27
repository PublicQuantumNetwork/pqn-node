"""Tests for the unattended Share a Secret Message game: ``POST /qkd/unattended``.

Per the plan's testing rule, kind 2: the leader is the real Node with its waveplate faked
through ``hardware.Client``, while the follower and the time tagger answer on an
``httpx.MockTransport``. The helpers are the SSM Operation-mode tests', reused: the fake
follower grows the endpoints the leader of an unattended game calls — being taken into the
follower role with its own Answers, handing over its key bits, and being reset.

Decision 11: Whobot sends both players' Answers to the leader, which puts the follower in
the follower role without asking anyone, always runs whatever either Node is doing, plays
the existing loop, resets coordination on both Nodes and returns the result. Decision 13:
in entanglement mode that result is the leader's inferred bits and the follower's actual
bits, one entry per question; in correlation mode it is each question's rate and
Coincidence level. The game takes any number of questions, independent of
``bitstring_length`` and the GUI's question order.
"""

# F401/F811: the SSM test module's fixtures are re-exported here by import, so that the
# tests below can take them as pytest arguments under their own names; ruff reads that
# pattern as an unimportable redefinition.
# ruff: noqa: F401, F811

import json
from collections.abc import Callable
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pqn_hardware.measurement import MeasurementConfig

from pqn_node.api.routes import qkd
from pqn_node.constants import OperationMode
from pqn_node.constants import QKDEncodingBasis
from pqn_node.core.config import NodeRole
from pqn_node.core.config import NodeState
from pqn_node.core.config import settings
from pqn_node.main import app
from tests.pytest.test_ssm_operation_modes import CALIBRATED
from tests.pytest.test_ssm_operation_modes import LEFT
from tests.pytest.test_ssm_operation_modes import RIGHT
from tests.pytest.test_ssm_operation_modes import FakeFollower
from tests.pytest.test_ssm_operation_modes import FakePair
from tests.pytest.test_ssm_operation_modes import FixedBits
from tests.pytest.test_ssm_operation_modes import RateSequence
from tests.pytest.test_ssm_operation_modes import _no_random_bit
from tests.pytest.test_ssm_operation_modes import calibrated as ssm_calibrated
from tests.pytest.test_ssm_operation_modes import game_state as ssm_game_state
from tests.pytest.test_ssm_operation_modes import make_game as ssm_make_game


@pytest.fixture(autouse=True)
def calibrated_angles(ssm_calibrated: dict[str, float]) -> None:
    """Set the Calibrated angles on the live settings for every unattended test, as for every SSM test."""


class UnattendedFollower(FakeFollower):
    """The other Node, answering the unattended endpoints the leader calls too.

    Records its takeover — the leader putting it in the follower role with its own Answers —
    the key bits it hands over, and how many times coordination was reset on it.
    """

    def __init__(
        self,
        *args: Any,
        fail_single_bit: bool = False,
        fail_follower_bits: bool = False,
        fail_reset: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fail_single_bit = fail_single_bit
        self.fail_follower_bits = fail_follower_bits
        self.fail_reset = fail_reset
        self.takeover: dict[str, Any] | None = None
        self.key_bits: list[int] = []
        self.resets = 0
        self.cancellations = 0

    @staticmethod
    def _gone() -> httpx.Response:
        """Answer as a follower that is not there: the same 500 for every endpoint it can stop answering."""
        return httpx.Response(500, json="the follower is gone")

    def _answer_coordination(self, path: str) -> httpx.Response | None:
        """Answer a coordination call this Node's own reset path makes, or None when the call is not one."""
        if path == "/coordination/reset_coordination_state":
            self.resets += 1
            return self._gone() if self.fail_reset else httpx.Response(200, json={"status": "acknowledged"})
        if path == "/coordination/protocol_cancelled":
            self.cancellations += 1
            return httpx.Response(200, json={"status": "acknowledged"})
        return None

    def respond(self, request: httpx.Request) -> httpx.Response | None:
        path = request.url.path
        if path == "/qkd/unattended/follower":
            self.takeover = {
                "leaders_name": request.url.params["leaders_name"],
                "answers": json.loads(request.read()),
            }
            return httpx.Response(200, json=True)
        if path == "/qkd/unattended/follower_bits":
            return self._gone() if self.fail_follower_bits else httpx.Response(200, json=self.key_bits)
        coordination = self._answer_coordination(path)
        if coordination is not None:
            return coordination
        if path == "/qkd/single_bit" and self.fail_single_bit:
            return self._gone()
        if path == "/qkd/request_basis_list":
            response = super().respond(request)
            if response is not None and response.status_code == HTTPStatus.OK:
                # The follower's own key bits, one per question: the actual bit it recorded
                # for every question, which the leader compares with the bits it inferred.
                self.key_bits = list(self.bits)
            return response
        return super().respond(request)


def run_unattended(
    game: FakePair, leader_answers: list[str], follower_answers: list[str], **params: Any
) -> httpx.Response:
    """Start one unattended SSM game on the leader, as Whobot does."""
    return game.client.post(
        "/qkd/unattended",
        params={"timetagger_address": "tt.invalid:9000", **params},
        json={"leader_answers": leader_answers, "follower_answers": follower_answers},
    )


def assert_follower_never_asked(follower: UnattendedFollower) -> None:
    """Decision 11: the follower is put in the follower role without anyone being asked."""
    asked = {"/coordination/follow_requested", "/coordination/collect_follower"}
    assert all(request.url.path not in asked for request in follower.requests)


def assert_coordination_reset(follower: UnattendedFollower, state: NodeState) -> None:
    """Decision 11: coordination is reset on both Nodes, whether the game completed or not.

    The follower's reset is the leader's call to its reset endpoint, plus the cancellation
    the leader's own reset notifies it of; the leader's is its whole coordination state.
    """
    assert follower.resets == 1
    assert follower.cancellations == 1
    assert state.role is NodeRole.INDEPENDENT
    assert state.followers_address == ""
    assert state.leaders_address == ""
    assert state.qkd_leader_basis_list == []
    assert state.qkd_bit_list == []


def play_without_a_random_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the live settings in correlation mode, where no random bit is ever drawn."""
    monkeypatch.setattr(settings, "operation_mode", OperationMode.CORRELATION)
    monkeypatch.setattr(qkd, "secrets", SimpleNamespace(randbits=_no_random_bit))


# --------------------------------------------------------------------------------------
# The game, in both Operation modes.
# --------------------------------------------------------------------------------------


def test_an_unattended_entanglement_game_end_to_end(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unattended entanglement game: both sides' key bits, on a leader mid-game as someone's follower.

    Decisions 11 and 13: the leader takes over whatever either Node was doing, puts the
    follower in the follower role with its own Answers without asking anyone, and returns
    its inferred bits next to the follower's actual bits — one entry per question, in
    question order. All four questions matched here, so both lists have all four. The counts
    are scripted, so the leader's bits follow the loop's outcome formula whatever the two
    Answers were; the leftover bits of the game taken over must not shift them.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([0, 1, 0, 1]))
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    # This Node was mid-game as someone else's follower, with bits left over from a game
    # before that: the unattended game always runs, and takes over.
    ssm_game_state.role = NodeRole.FOLLOWER
    ssm_game_state.leaders_address = "other-leader.invalid:8000"
    ssm_game_state.qkd_leader_basis_list = [QKDEncodingBasis.DA]
    ssm_game_state.qkd_bit_list = [1, 1, 1]
    follower = UnattendedFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT, LEFT, RIGHT, RIGHT],
        bits=[1, 0, 1, 0],
        # Rates 3000, 0, 3000 and 3000 cps at the 2 s integration: counts above the
        # discriminating threshold on every question but the second.
        source=RateSequence([3000.0, 0.0, 3000.0, 3000.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, LEFT, RIGHT, RIGHT], [LEFT, LEFT, RIGHT, RIGHT])

    assert response.status_code == HTTPStatus.OK
    # The leader's inferred bits, from the loop's outcome formula on the scripted counts:
    # ((above ^ choice) ^ (1 - bell_state)) ^ basis, one per question.
    assert response.json() == {
        "mode": "entanglement",
        "leader_bits": [0, 0, 1, 0],
        "follower_bits": [1, 0, 1, 0],
    }
    # The follower was put in the follower role with its own Answers, without being asked.
    assert follower.takeover == {"leaders_name": settings.node_name, "answers": [LEFT, LEFT, RIGHT, RIGHT]}
    assert_follower_never_asked(follower)
    # Both sides played their own Answers, one handshake per question, and the leader's own
    # bits moved its waveplate: H, V, D, A for choices 0, 1, 0, 1 in the HV, HV, DA, DA bases.
    assert game.plate.moves == [CALIBRATED[name] for name in ["h", "v", "d", "a"]]
    paths = [request.url.path for request in follower.requests]
    assert paths.count("/qkd/single_bit") == len(follower.answers)
    assert "/qkd/request_basis_list" in paths
    assert "/qkd/unattended/follower_bits" in paths
    # The follower is ready when its setup call answers, so nothing polls it.
    assert "/qkd/is_follower_ready" not in paths
    assert_coordination_reset(follower, ssm_game_state)


def test_an_unattended_entanglement_game_reports_a_bit_for_every_question(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both lists have one bit per question, whatever the two Answers were (decision 13).

    Three questions where only the first matched, so both lists carry all three questions:
    the leader's inferred bit for each, and the follower's actual bit for each, in question
    order. Whobot knows the Answers it sent, so picking the matched questions — and reading
    the two lists as an error rate — is its job, not the Node's. The two lists are derived
    independently, and here they disagree on the questions that matter.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([1, 0, 1]))
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    follower = UnattendedFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT, LEFT, RIGHT],
        bits=[0, 1, 0],
        source=RateSequence([3000.0, 3000.0, 3000.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, RIGHT, LEFT], [LEFT, LEFT, RIGHT])

    assert response.status_code == HTTPStatus.OK
    # Counts all above threshold (above = 1), choices [1, 0, 1] in the HV, DA, HV bases:
    # ((1 ^ choice) ^ 1) ^ basis is 1, 1, 1. The follower's actual bits are its own [0, 1, 0].
    assert response.json() == {"mode": "entanglement", "leader_bits": [1, 1, 1], "follower_bits": [0, 1, 0]}
    assert_follower_never_asked(follower)
    assert_coordination_reset(follower, ssm_game_state)


def test_an_unattended_correlation_game_end_to_end(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unattended correlation game: each question's rate and Coincidence level, all four Answer combinations.

    Decisions 1, 11 and 13: the two Answers move only their own waveplates, the rate is the
    count over the integration time it was measured with, and the nearest Reference rate
    classifies it. VV and DD agree (high, low), VD and DV do not (medium).
    """
    play_without_a_random_bit(monkeypatch)
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    follower = UnattendedFollower(
        mode=OperationMode.CORRELATION,
        answers=[LEFT, RIGHT, LEFT, RIGHT],
        source=RateSequence([3000.0, 1500.0, 1500.0, 750.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, LEFT, RIGHT, RIGHT], [LEFT, RIGHT, LEFT, RIGHT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "mode": "correlation",
        "rates": [3000.0, 1500.0, 1500.0, 750.0],
        "levels": ["high", "medium", "medium", "low"],
    }
    assert follower.takeover == {"leaders_name": settings.node_name, "answers": [LEFT, RIGHT, LEFT, RIGHT]}
    assert_follower_never_asked(follower)
    # One handshake per question, and neither the follower's Answers nor its bits are ever
    # fetched: the photons do all the telling (decision 3).
    paths = [request.url.path for request in follower.requests]
    assert paths.count("/qkd/single_bit") == len(follower.answers)
    assert "/qkd/request_basis_list" not in paths
    assert "/qkd/unattended/follower_bits" not in paths
    assert "/qkd/is_follower_ready" not in paths
    assert game.integration_times == [2.0, 2.0, 2.0, 2.0]
    assert_coordination_reset(follower, ssm_game_state)


def test_an_unattended_game_can_integrate_for_another_time(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unattended game integrates for the time it is given, not either config's, and the rates follow.

    The CHSH config integrates for 2 s and the QKD one for 3 s; the game is asked for 1 s.
    Counts of 3000 and 1500 in that second are rates of 3000 and 1500 cps — dividing by
    either config's time would halve or third them, and misclassify the second question.
    """
    play_without_a_random_bit(monkeypatch)
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    monkeypatch.setattr(settings.qkd_settings, "measurement_config", MeasurementConfig(integration_time_s=3))
    follower = UnattendedFollower(
        mode=OperationMode.CORRELATION,
        answers=[LEFT, LEFT],
        source=RateSequence([3000.0, 1500.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, LEFT], [LEFT, LEFT], integration_time_s=1)

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"mode": "correlation", "rates": [3000.0, 1500.0], "levels": ["high", "medium"]}
    assert game.integration_times == [1.0, 1.0]
    assert_coordination_reset(follower, ssm_game_state)


# --------------------------------------------------------------------------------------
# Always runs, and resets coordination on both Nodes — also on failure.
# --------------------------------------------------------------------------------------


def test_an_unattended_game_that_fails_still_resets_coordination(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A game that does not complete still resets coordination on both Nodes (decisions 11, 13).

    The follower stops answering handshakes mid-game, so the game fails; the leader still
    resets the follower and itself, and the failure reaches the caller as a 500.
    """
    play_without_a_random_bit(monkeypatch)
    follower = UnattendedFollower(
        mode=OperationMode.CORRELATION,
        answers=[LEFT, LEFT],
        fail_single_bit=True,
        source=RateSequence([3000.0, 3000.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, LEFT], [LEFT, LEFT])

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert follower.takeover is not None  # the game did start, and did take the follower over
    assert_coordination_reset(follower, ssm_game_state)


def test_an_unattended_entanglement_game_that_fails_fetching_bits_still_resets_coordination(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the game — reading the follower's bits back — still resets coordination on both Nodes.

    The follower plays its whole half and then refuses the key-bits read, the last step of
    an entanglement game. The failure is inside the try, so the leader still resets the
    follower and itself, and reaches the caller as a 500 (decisions 11, 13).
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([0, 1]))
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    follower = UnattendedFollower(
        mode=OperationMode.ENTANGLEMENT,
        answers=[LEFT, LEFT],
        bits=[1, 0],
        fail_follower_bits=True,
        source=RateSequence([3000.0, 3000.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, LEFT], [LEFT, LEFT])

    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    # The game ran to its end: the follower was taken over and answered every handshake.
    assert follower.takeover is not None
    paths = [request.url.path for request in follower.requests]
    assert paths.count("/qkd/single_bit") == len(follower.answers)
    assert "/qkd/unattended/follower_bits" in paths
    assert_coordination_reset(follower, ssm_game_state)


def test_a_follower_that_will_not_reset_still_lets_the_game_finish(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follower's reset is best-effort: it failing does not fail a game that played (decision 11).

    The follower refuses its own coordination reset, but the game has a result, so the leader
    still returns it with a 200, still tried the follower's reset, and still fully reset its
    own coordination — a follower that will not be reset cannot be reset by trying harder.
    """
    play_without_a_random_bit(monkeypatch)
    monkeypatch.setattr(settings.chsh_settings, "measurement_config", MeasurementConfig(integration_time_s=2))
    follower = UnattendedFollower(
        mode=OperationMode.CORRELATION,
        answers=[LEFT, LEFT],
        fail_reset=True,
        source=RateSequence([3000.0, 3000.0]),
    )
    game = ssm_make_game(follower)

    response = run_unattended(game, [LEFT, LEFT], [LEFT, LEFT])

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"mode": "correlation", "rates": [3000.0, 3000.0], "levels": ["high", "high"]}
    # The reset was attempted (and refused), and this Node reset itself regardless.
    assert follower.resets == 1
    assert ssm_game_state.role is NodeRole.INDEPENDENT
    assert ssm_game_state.followers_address == ""
    assert ssm_game_state.leaders_address == ""
    assert ssm_game_state.qkd_leader_basis_list == []
    assert ssm_game_state.qkd_bit_list == []


def test_unattended_integration_time_must_be_positive(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive integration time is a 422 that reaches neither Node.

    Decision 14 makes the integration time a Run SSM field; a zero or negative one is not a
    time, and is refused before the follower is touched, leaving everything as it was.
    """
    play_without_a_random_bit(monkeypatch)
    follower = UnattendedFollower(mode=OperationMode.CORRELATION, answers=[LEFT])
    game = ssm_make_game(follower)

    zero = run_unattended(game, [LEFT], [LEFT], integration_time_s=0)
    negative = run_unattended(game, [LEFT], [LEFT], integration_time_s=-1)

    assert zero.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert negative.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert follower.requests == []
    assert follower.takeover is None
    assert ssm_game_state.role is NodeRole.INDEPENDENT


def test_unattended_answers_are_validated_before_anything_runs(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unequal, empty or unknown Answers are a 400 that reaches neither Node.

    Both players' Answers are checked before the follower is touched, so a malformed game
    leaves everything exactly as it was.
    """
    play_without_a_random_bit(monkeypatch)
    follower = UnattendedFollower(mode=OperationMode.CORRELATION, answers=[LEFT])
    game = ssm_make_game(follower)

    unequal = run_unattended(game, [LEFT, LEFT], [LEFT])
    empty = run_unattended(game, [], [])
    unknown_leader_answer = run_unattended(game, ["c"], [LEFT])
    unknown_follower_answer = run_unattended(game, [LEFT], ["c"])

    assert unequal.status_code == HTTPStatus.BAD_REQUEST
    assert empty.status_code == HTTPStatus.BAD_REQUEST
    assert unknown_leader_answer.status_code == HTTPStatus.BAD_REQUEST
    assert unknown_follower_answer.status_code == HTTPStatus.BAD_REQUEST
    assert follower.requests == []
    assert follower.takeover is None
    assert ssm_game_state.role is NodeRole.INDEPENDENT


def test_an_unattended_game_without_a_configured_follower_is_a_503(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Node with no follower configured cannot run the game, and says so before touching anything.

    Decision 15: the unattended game leads, so it needs this Node's configured follower; with
    none set it is the same 503 a Game with no follower gives.
    """
    play_without_a_random_bit(monkeypatch)
    follower = UnattendedFollower(mode=OperationMode.CORRELATION, answers=[LEFT])
    game = ssm_make_game(follower)
    monkeypatch.setattr(settings, "follower_node_address", None)

    response = run_unattended(game, [LEFT], [LEFT])

    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert follower.requests == []
    assert follower.takeover is None


# --------------------------------------------------------------------------------------
# The follower's side: taken into the follower role, and its key bits read back.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("mid_game_role", [NodeRole.FOLLOWER, NodeRole.LEADER])
def test_the_unattended_follower_endpoint_takes_over_a_game(
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
    mid_game_role: NodeRole,
) -> None:
    """Whatever this Node was doing, the leader's call puts it in the follower role with its own Answers.

    Decision 11: nobody is asked. A game in progress — as a follower halfway through its
    questions, or as a leader — is taken over: the new game's Answers replace the old one's,
    and its question counter starts from the first question, recording nothing from before.
    """
    monkeypatch.setattr(ssm_game_state, "role", mid_game_role)
    monkeypatch.setattr(ssm_game_state, "leaders_name", "someone else")
    monkeypatch.setattr(ssm_game_state, "leaders_address", "someone-else.invalid:8000")
    monkeypatch.setattr(ssm_game_state, "qkd_follower_basis_list", [QKDEncodingBasis.DA, QKDEncodingBasis.HV])
    monkeypatch.setattr(ssm_game_state, "qkd_single_bit_current_index", 2)
    monkeypatch.setattr(ssm_game_state, "qkd_request_basis_list", [QKDEncodingBasis.DA])
    monkeypatch.setattr(ssm_game_state, "qkd_request_bit_list", [1])
    monkeypatch.setattr(ssm_game_state, "qkd_resulting_bit_list", [1, 1])
    client = TestClient(app)

    response = client.post(
        "/qkd/unattended/follower",
        params={"leaders_name": "the-leader", "leaders_port": 8000},
        json=[LEFT, RIGHT, LEFT],
    )

    assert response.status_code == HTTPStatus.OK
    assert response.json() is True
    assert ssm_game_state.role is NodeRole.FOLLOWER
    assert ssm_game_state.leaders_name == "the-leader"
    assert ssm_game_state.leaders_address == "testclient:8000"
    assert ssm_game_state.qkd_follower_basis_list == [QKDEncodingBasis.HV, QKDEncodingBasis.DA, QKDEncodingBasis.HV]
    assert ssm_game_state.qkd_single_bit_current_index == 0
    assert ssm_game_state.qkd_request_basis_list == []
    assert ssm_game_state.qkd_request_bit_list == []
    assert ssm_game_state.qkd_resulting_bit_list == []


def test_the_unattended_follower_endpoint_refuses_unknown_answers(
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Answer that is neither left nor right is a 400 that puts this Node in no role at all."""
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    client = TestClient(app)

    response = client.post(
        "/qkd/unattended/follower",
        params={"leaders_name": "the-leader", "leaders_port": 8000},
        json=["c"],
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert ssm_game_state.role is NodeRole.INDEPENDENT
    assert ssm_game_state.qkd_follower_basis_list == []


def test_the_follower_bits_endpoint_returns_its_own_key_bits(
    ssm_make_game: Callable[[FakeFollower], FakePair],
    ssm_game_state: NodeState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The follower's key bits are its own bits, one per question (decision 13).

    This Node is taken into the follower role as the unattended leader does it, plays its
    half of a three-question entanglement game one handshake at a time, and hands the leader
    its Answers as today's game does. All three of its own bits come back, one per question,
    not only the first question's, whose two Answers happened to match. The Answers fetch
    itself answers exactly as it always has.
    """
    monkeypatch.setattr(settings, "operation_mode", OperationMode.ENTANGLEMENT)
    monkeypatch.setattr(qkd, "secrets", FixedBits([0, 1, 0]))
    monkeypatch.setattr(ssm_game_state, "qkd_resulting_bit_list", [])
    game = ssm_make_game(
        UnattendedFollower(mode=OperationMode.ENTANGLEMENT, answers=[LEFT, RIGHT, LEFT], bits=[0, 1, 0])
    )

    takeover = game.client.post(
        "/qkd/unattended/follower",
        params={"leaders_name": "the-leader", "leaders_port": 8000},
        json=[LEFT, RIGHT, LEFT],
    )
    handshakes = [game.client.post("/qkd/single_bit") for _ in range(3)]
    bases = game.client.post("/qkd/request_basis_list", json=["HV", "HV", "DA"])

    bits = game.client.get("/qkd/unattended/follower_bits")

    assert takeover.status_code == HTTPStatus.OK
    assert all(handshake.status_code == HTTPStatus.OK for handshake in handshakes)
    assert bases.status_code == HTTPStatus.OK
    assert bases.json() == ["HV", "DA", "HV"]
    assert bits.status_code == HTTPStatus.OK
    assert bits.json() == [0, 1, 0]
