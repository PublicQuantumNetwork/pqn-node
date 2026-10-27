"""Tests for the flow: which branch ``dispatch`` takes, and what ``execute`` guarantees.

Driven by ``WhobotSpy``, a Chat Platform that draws nothing and records what it was asked
to draw. That is the whole point of the abstract surface — the flow can be pinned without
Slack, without a socket, and without a Node.

The invariant these tests exist to defend is that **every announcement is followed by a
result**, however the Action ends: normally, by raising, by timing out, or by cancellation.

Two things every test here needs. It runs in a temp working directory, because
``WhobotSettings`` reads ``./whobot.toml`` *before* its keyword arguments, so a real config
in the repo root would otherwise decide what the fleet is. And ``resolve_nodes`` is faked,
because dispatch consults the registry over HTTP on every ``scope=ONE`` click.
"""

import asyncio
import csv
import json
import logging
import re
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from pqn_node.api.routes.health import HealthStatus
from pqn_node.constants import OperationMode
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import PolarizationAngles
from pqn_node.core.config import ReferenceRates
from pqn_whobot.actions import DEFAULT_TIMEOUT_S
from pqn_whobot.actions import Action
from pqn_whobot.actions import ActionResult
from pqn_whobot.actions import DigestResult
from pqn_whobot.actions import Field
from pqn_whobot.actions import NodeDigest
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import ReplyHandle
from pqn_whobot.actions import Report
from pqn_whobot.actions import Scope
from pqn_whobot.actions import Section
from pqn_whobot.actions import Status
from pqn_whobot.actions import action
from pqn_whobot.actions import prefill
from pqn_whobot.config import NodeEntry
from pqn_whobot.config import WhobotSettings
from pqn_whobot.config import update_config
from pqn_whobot.node_client import NodeClient
from pqn_whobot.registry import Node
from pqn_whobot.whobot import GAME_TITLES
from pqn_whobot.whobot import OPERATION_MODE_LABEL
from pqn_whobot.whobot import Whobot
from pqn_whobot.whobot import _bell_verdict
from pqn_whobot.whobot import _hardware_section
from pqn_whobot.whobot import one_game_budget
from pqn_whobot.whobot import one_node_budget
from pqn_whobot.whobot import ssm_budget

ALICE = "http://node-a.invalid:9000"
BOB = "http://node-b.invalid:9000"

REACHABLE = [
    Node(api_url=ALICE, name="uiuc-public-left", reachable=True, latency_ms=18.0),
    Node(api_url=BOB, name="ufl-public-right", reachable=True, latency_ms=24.0),
]

SCHEDULED = ReplyHandle()
"""Stands in for the digest channel: what a run nobody clicked for is handed."""

DISTINCT_FAILURE_MODES = 3
"""Raising, timing out and being interrupted: three failures an operator must tell apart."""

FLEET: list[Node] = []
"""What the faked registry currently reports. Set by ``spy``, reset between tests."""


# --------------------------------------------------------------------------------------
# The spy Chat Platform.
# --------------------------------------------------------------------------------------


@dataclass
class Drawn:
    """What a Chat Platform was asked to draw, in order."""

    calls: list[str] = field(default_factory=list)
    menu: list[Action] = field(default_factory=list)
    targets: list[Node] = field(default_factory=list)
    initial: dict[str, object] = field(default_factory=dict)
    notes: list[str | None] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    replies: list[ReplyHandle] = field(default_factory=list)


class WhobotSpy(Whobot):
    """A Chat Platform that renders nothing and remembers everything."""

    def __init__(self, settings: WhobotSettings) -> None:
        super().__init__(settings)
        self.drawn = Drawn()

    async def show_menu(self, actions: list[Action], handle: ReplyHandle, note: str | None = None) -> None:
        self.drawn.calls.append("show_menu")
        self.drawn.menu = actions
        self.drawn.notes.append(note)

    async def ask_for_target(
        self, act: Action, nodes: list[Node], handle: ReplyHandle, note: str | None = None
    ) -> None:
        self.drawn.calls.append("ask_for_target")
        self.drawn.targets = nodes
        self.drawn.notes.append(note)

    async def ask_for_params(
        self, act: Action, pending: PendingInvocation, initial: dict[str, object], handle: ReplyHandle
    ) -> None:
        self.drawn.calls.append("ask_for_params")
        self.drawn.initial = initial

    async def ask_to_confirm(self, act: Action, pending: PendingInvocation, handle: ReplyHandle) -> None:
        self.drawn.calls.append("ask_to_confirm")

    async def announce_start(self, act: Action, pending: PendingInvocation, handle: ReplyHandle) -> ReplyHandle:
        self.drawn.calls.append("announce_start")
        return handle

    async def post_result(self, act: Action, result: ActionResult, reply: ReplyHandle) -> None:
        self.drawn.calls.append("post_result")
        self.drawn.results.append(result)
        self.drawn.replies.append(reply)

    def scheduled_handle(self) -> ReplyHandle:
        return SCHEDULED


class FlowSpy(WhobotSpy):
    """Adds the Action shapes the three real ones do not cover.

    These are declared here rather than in ``whobot.py`` for the reason the plan gives for
    not shipping a destructive Action early: no production Action should exist to serve a
    test. A test-only subclass covers the branches the real Actions do not reach yet.
    """

    @action(label="Quiet", description="Does nothing, quickly.")
    async def quiet(self) -> Report:
        return Report(status=Status.OK, title="Quiet")

    @action(label="Needs A Form")
    async def needs_form(self, *, loud: bool = False) -> Report:
        return Report(status=Status.OK, title=f"loud={loud}")

    @action(label="Dangerous", destructive=True)
    async def dangerous(self) -> Report:
        return Report(status=Status.OK, title="Dangerous")

    @action(label="Raises")
    async def raises(self) -> Report:
        msg = "the Node caught fire"
        raise RuntimeError(msg)

    @action(label="Slow", timeout_s=0.01)
    async def slow(self) -> Report:
        await asyncio.sleep(10)
        return Report(status=Status.OK, title="Slow")

    @action(label="Blocks")
    async def blocks(self) -> Report:
        await asyncio.sleep(10)
        return Report(status=Status.OK, title="Blocks")

    @action(label="Prefilled", scope=Scope.ONE)
    async def prefilled(self, node: Node, *, loud: bool = False) -> Report:
        return Report(status=Status.OK, title=f"{node.api_url} loud={loud}")

    @prefill(prefilled)
    async def _prefilled_fill(self, node: Node) -> dict[str, object]:
        return {"loud": True}

    @action(label="Prefill Explodes", scope=Scope.ONE)
    async def prefill_explodes(self, node: Node, *, loud: bool = False) -> Report:
        return Report(status=Status.OK, title="Prefill Explodes")

    @prefill(prefill_explodes)
    async def _explode(self, node: Node) -> dict[str, object]:
        msg = "the Node did not answer"
        raise RuntimeError(msg)


class ClientSpy(FlowSpy):
    """A Whobot whose Node API calls are answered by a handler rather than the network."""

    handler: Callable[[httpx.Request], httpx.Response]

    def _client(self, node: Node) -> NodeClient:
        return NodeClient(node.api_url, transport=httpx.MockTransport(self.handler))


# --------------------------------------------------------------------------------------
# Harness.
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _in_a_temp_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a real ./whobot.toml from deciding what these tests see.

    ``WhobotSettings`` reads the file *before* its keyword arguments, so a developer's own
    config in the repo root would silently replace every fleet built here.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _registry_is_not_the_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answer the registry from ``FLEET``, filtered by what the settings actually register."""

    async def fake(settings: WhobotSettings) -> list[Node]:
        registered = {entry.api_url for entry in settings.nodes}
        return [node for node in FLEET if node.api_url in registered]

    monkeypatch.setattr("pqn_whobot.whobot.resolve_nodes", fake)
    FLEET[:] = REACHABLE
    yield
    FLEET[:] = []


def settings_for(*urls: str, **overrides: object) -> WhobotSettings:
    return WhobotSettings(nodes=[NodeEntry(api_url=url) for url in urls], **overrides)


def spy(*urls: str, nodes: list[Node] | None = None) -> FlowSpy:
    """Build a Whobot registered for ``urls``, with the fleet the registry will report."""
    if nodes is not None:
        FLEET[:] = nodes
    return FlowSpy(settings_for(*urls))


async def dispatched(bot: WhobotSpy, pending: PendingInvocation) -> None:
    """Run one click, then let anything it spawned finish."""
    await bot.dispatch(pending, ReplyHandle())
    await bot.shutdown(grace_s=1.0)


def run(bot: FlowSpy, pending: PendingInvocation) -> FlowSpy:
    asyncio.run(dispatched(bot, pending))
    return bot


def only_report(bot: WhobotSpy) -> Report:
    """Return the single Report the bot posted, asserting there is exactly one."""
    assert len(bot.drawn.results) == 1
    result = bot.drawn.results[0]
    assert isinstance(result, Report)
    return result


# --------------------------------------------------------------------------------------
# dispatch: the five branches.
# --------------------------------------------------------------------------------------


def test_nothing_chosen_shows_the_menu() -> None:
    """``action=None`` is why opening the menu needs no special case in a handler."""
    bot = run(spy(ALICE), PendingInvocation())
    assert bot.drawn.calls == ["show_menu"]


def test_the_menu_comes_from_the_class_not_a_hardcoded_list() -> None:
    """The extensibility claim: a method becomes a menu entry with no menu code edited."""
    bot = run(spy(ALICE), PendingInvocation())
    labels = [act.label for act in bot.drawn.menu]
    assert "List Nodes" in labels
    assert "Dangerous" in labels  # declared only on FlowSpy, and it appears anyway
    assert labels == [act.label for act in bot.actions.values()]


def test_a_scope_none_action_runs_straight_away() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="quiet"))
    assert bot.drawn.calls == ["announce_start", "post_result"]


def test_a_scope_one_action_asks_for_a_target_first() -> None:
    bot = run(spy(ALICE, BOB), PendingInvocation(action="node_info"))
    assert bot.drawn.calls == ["ask_for_target"]
    assert [node.api_url for node in bot.drawn.targets] == [ALICE, BOB]
    assert bot.drawn.notes == [None]  # nothing went wrong; the note is for staleness only


def test_an_action_with_parameters_asks_for_them() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="needs_form"))
    assert bot.drawn.calls == ["ask_for_params"]


def test_a_destructive_action_asks_to_confirm_before_running() -> None:
    """Selecting Reboot from a dropdown must never reboot anything."""
    bot = run(spy(ALICE), PendingInvocation(action="dangerous"))
    assert bot.drawn.calls == ["ask_to_confirm"]
    assert "announce_start" not in bot.drawn.calls


def test_a_confirmed_destructive_action_runs() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="dangerous", confirmed=True))
    assert bot.drawn.calls == ["announce_start", "post_result"]


def test_the_branches_are_taken_in_order_target_then_params() -> None:
    """A form must not open before its Node is known, or the prefill has nothing to read."""
    bot = run(spy(ALICE, BOB), PendingInvocation(action="prefilled"))
    assert bot.drawn.calls == ["ask_for_target"]


def test_supplied_parameters_reach_the_action() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="needs_form", params={"loud": True}))
    assert bot.drawn.calls == ["announce_start", "post_result"]
    assert only_report(bot).title == "loud=True"


# --------------------------------------------------------------------------------------
# dispatch: a stale payload re-renders rather than raising.
# --------------------------------------------------------------------------------------


def test_an_action_that_no_longer_exists_re_renders_the_menu_with_a_note() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="deleted_last_week"))
    assert bot.drawn.calls == ["show_menu"]
    assert bot.drawn.notes[0] is not None
    assert "deleted_last_week" in bot.drawn.notes[0]


def test_a_node_no_longer_registered_re_renders_the_target_list_with_a_note() -> None:
    """The payload names a Node the registry has since dropped."""
    bot = run(spy(ALICE), PendingInvocation(action="node_info", node_url=BOB))
    assert bot.drawn.calls == ["ask_for_target"]
    assert bot.drawn.notes[0] is not None
    assert BOB in bot.drawn.notes[0]
    assert [node.api_url for node in bot.drawn.targets] == [ALICE]


def test_no_action_runs_against_a_node_the_payload_did_not_name() -> None:
    """Why ``api_url`` is the identifier rather than a registry index."""
    bot = run(spy(ALICE), PendingInvocation(action="node_info", node_url=BOB))
    assert "announce_start" not in bot.drawn.calls


# --------------------------------------------------------------------------------------
# execute: every announcement gets a result.
# --------------------------------------------------------------------------------------


def test_a_raising_action_becomes_a_failed_result() -> None:
    """An Action may not take the process down, ever."""
    bot = run(spy(ALICE), PendingInvocation(action="raises"))
    assert bot.drawn.calls == ["announce_start", "post_result"]
    assert only_report(bot).status is Status.FAIL


def test_a_timing_out_action_becomes_a_failed_result_naming_the_timeout() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="slow"))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.summary is not None
    assert "imed out" in result.summary


def test_a_cancelled_action_still_reports_itself() -> None:
    """An orphaned announcement is worse than a clear failure, so shutdown must not orphan one."""

    async def scenario() -> FlowSpy:
        bot = spy(ALICE)
        await bot.dispatch(PendingInvocation(action="blocks"), ReplyHandle())
        await asyncio.sleep(0)  # let the task reach its first await
        await bot.shutdown(grace_s=0.01)
        return bot

    bot = asyncio.run(scenario())
    assert bot.drawn.calls == ["announce_start", "post_result"]
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.summary is not None
    assert "nterrupted" in result.summary


def test_the_three_failure_modes_are_distinguishable() -> None:
    """Raising, timing out and being interrupted must not read the same to an operator."""

    async def scenario() -> list[str]:
        summaries = []
        for name in ("raises", "slow"):
            bot = spy(ALICE)
            await dispatched(bot, PendingInvocation(action=name))
            summary = only_report(bot).summary
            assert summary is not None
            summaries.append(summary)

        interrupted = spy(ALICE)
        await interrupted.dispatch(PendingInvocation(action="blocks"), ReplyHandle())
        await asyncio.sleep(0)
        await interrupted.shutdown(grace_s=0.01)
        summary = only_report(interrupted).summary
        assert summary is not None
        summaries.append(summary)
        return summaries

    assert len(set(asyncio.run(scenario()))) == DISTINCT_FAILURE_MODES


# --------------------------------------------------------------------------------------
# Task bookkeeping and shutdown.
# --------------------------------------------------------------------------------------


def test_a_running_action_is_held_so_it_cannot_be_garbage_collected() -> None:
    """Python collects a task nobody references, so the set is load-bearing."""

    async def scenario() -> None:
        bot = spy(ALICE)
        await bot.dispatch(PendingInvocation(action="blocks"), ReplyHandle())
        assert len(bot._tasks) == 1  # noqa: SLF001
        await bot.shutdown(grace_s=0.01)

    asyncio.run(scenario())


def test_a_finished_action_is_dropped_from_the_task_set() -> None:
    async def scenario() -> None:
        bot = spy(ALICE)
        await dispatched(bot, PendingInvocation(action="quiet"))
        assert bot._tasks == set()  # noqa: SLF001

    asyncio.run(scenario())


def test_shutdown_refuses_new_work() -> None:
    """A click arriving mid-shutdown must not start an Action at all."""

    async def scenario() -> FlowSpy:
        bot = spy(ALICE)
        await bot.shutdown(grace_s=0.01)
        await bot.dispatch(PendingInvocation(action="quiet"), ReplyHandle())
        return bot

    bot = asyncio.run(scenario())
    assert bot.drawn.calls == []


# --------------------------------------------------------------------------------------
# Prefill.
# --------------------------------------------------------------------------------------


def test_a_form_without_a_prefill_opens_on_the_signature_defaults() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="needs_form"))
    assert bot.drawn.initial == {"loud": False}


def test_a_prefill_overrides_the_signature_defaults() -> None:
    bot = run(spy(ALICE), PendingInvocation(action="prefilled", node_url=ALICE))
    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == {"loud": True}


def test_a_failing_prefill_still_opens_the_form_on_its_defaults() -> None:
    """A form opening on defaults beats no form, so a dead Node must not block the modal."""
    bot = run(spy(ALICE), PendingInvocation(action="prefill_explodes", node_url=ALICE))
    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == {"loud": False}


# --------------------------------------------------------------------------------------
# list_nodes.
# --------------------------------------------------------------------------------------


def test_list_nodes_reports_one_row_per_registered_node() -> None:
    bot = run(spy(ALICE, BOB), PendingInvocation(action="list_nodes"))
    result = only_report(bot)
    assert result.summary == "2 of 2 reachable"
    assert [f.name for f in result.sections[0].fields] == ["uiuc-public-left", "ufl-public-right"]
    assert all(f.status is Status.OK for f in result.sections[0].fields)


def test_list_nodes_marks_an_unreachable_node_without_failing_the_other() -> None:
    dead = [REACHABLE[0], Node(api_url=BOB, reachable=False, error="ConnectError: refused")]
    bot = run(spy(ALICE, BOB, nodes=dead), PendingInvocation(action="list_nodes"))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.summary == "1 of 2 reachable"
    assert [f.status for f in result.sections[0].fields] == [Status.OK, Status.FAIL]


def test_list_nodes_says_so_when_the_registry_is_empty() -> None:
    bot = run(spy(), PendingInvocation(action="list_nodes"))
    result = only_report(bot)
    assert result.status is Status.WARN
    assert result.sections == []


def test_a_node_that_answers_without_a_name_is_a_warning_not_a_failure() -> None:
    """A partly-deployed fleet is normal; a status that cries wolf stops being read."""
    older = [Node(api_url=ALICE, reachable=True, warning="no node_name", latency_ms=12.0)]
    bot = run(spy(ALICE, nodes=older), PendingInvocation(action="list_nodes"))
    result = only_report(bot)
    assert result.status is Status.WARN
    assert result.sections[0].fields[0].status is Status.WARN


# --------------------------------------------------------------------------------------
# node_info and set_availability, against a mocked Node API.
# --------------------------------------------------------------------------------------


def with_node_api(handler: Callable[[httpx.Request], httpx.Response], *urls: str, **settings: object) -> ClientSpy:
    bot = ClientSpy(settings_for(*urls, **settings))
    bot.handler = handler
    return bot


def test_node_info_reports_what_the_node_says_about_itself() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"node_name": "uiuc-public-left", "follower_node_address": "10.0.0.9"})

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="node_info", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.OK
    assert [f.value for f in result.sections[0].fields] == ["uiuc-public-left", "10.0.0.9"]


def test_node_info_on_a_node_that_does_not_answer_is_one_failed_result() -> None:
    """One dead Node is a FAIL result, not a dead Action and not a dead process."""

    def handler(request: httpx.Request) -> httpx.Response:
        msg = "Connection refused"
        raise httpx.ConnectError(msg, request=request)

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="node_info", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.sections[0].error is not None


def test_set_availability_reports_each_game_as_on_or_off() -> None:
    """The rows answer "is this Game on", so an off Game reads as off, not as a failure."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"chsh": True, "qf": True, "ssm": False})

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(
        action="set_availability", node_url=ALICE, params={"chsh": True, "qf": True, "ssm": False}
    )
    asyncio.run(dispatched(bot, pending))
    result = only_report(bot)
    assert result.status is Status.OK
    assert [f.status for f in result.sections[0].fields] == [Status.ON, Status.ON, Status.OFF]
    assert [f.value for f in result.sections[0].fields] == ["available", "available", "not available"]


def test_a_game_switched_off_on_purpose_is_not_bad_news() -> None:
    """A report headlined WARN for an off Game trains an operator to ignore the headline."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"chsh": False, "qf": False, "ssm": False})

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(
        action="set_availability", node_url=ALICE, params={"chsh": False, "qf": False, "ssm": False}
    )
    asyncio.run(dispatched(bot, pending))
    result = only_report(bot)
    assert result.status is Status.OK
    assert result.summary is None


def test_set_availability_warns_when_the_node_could_not_apply_it() -> None:
    """The endpoint answers with *effective* availability, so asking is not getting.

    The Node answered, so this is not an unreachable Node — the Router or the follower lives
    on another machine, and a Game gated off by one of those is a WARN rather than an OFF:
    the write landed in config and comes back on its own, so the operator must be sent to
    look at the hardware rather than told the Game is simply off.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"chsh": False, "qf": True, "ssm": True})

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(
        action="set_availability", node_url=ALICE, params={"chsh": True, "qf": True, "ssm": True}
    )
    asyncio.run(dispatched(bot, pending))
    result = only_report(bot)
    assert result.status is Status.WARN
    assert result.sections[0].fields[0].status is Status.WARN
    assert "gated off" in result.sections[0].fields[0].value
    assert result.summary is not None
    assert "CHSH" in result.summary
    # The Games that did apply still read as plain state, so the one problem stands out.
    assert [f.status for f in result.sections[0].fields[1:]] == [Status.ON, Status.ON]


def test_the_availability_prefill_reads_the_nodes_current_flags() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"chsh": False, "qf": True, "ssm": False})

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="set_availability", node_url=ALICE)))
    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == {"chsh": False, "qf": True, "ssm": False}


def test_the_form_asks_about_every_game() -> None:
    """A Game added to the Node's model must also be added to ``set_availability``.

    The report loop reads ``model_fields`` and so picks a new Game up on its own; the form
    reads the signature and cannot. Without this, adding one would silently produce a form
    that never asks about it — the failure that a model-valued parameter used to prevent, at
    the cost of an expansion mechanism nothing else in the system wanted.
    """
    asked = {parameter.name for parameter in WhobotSpy.actions["set_availability"].parameters}
    assert asked == set(GamesAvailability.model_fields)


# --------------------------------------------------------------------------------------
# Change Operation mode and Change calibration, against a mocked Node API.
# --------------------------------------------------------------------------------------


def test_the_operation_mode_form_is_a_dropdown_of_the_two_modes() -> None:
    """Decision 16: an enum dropdown behind the Change Operation mode button.

    A dropdown of the enum's members is how the form cannot ask for a mode that does not
    exist.
    """
    act = WhobotSpy.actions["set_operation_mode"]
    parameter = act.parameters[0]

    assert act.label == "Change Operation mode"
    assert parameter.annotation is OperationMode
    assert parameter.default is OperationMode.ENTANGLEMENT


def test_the_operation_mode_form_opens_on_the_nodes_mode() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json="correlation")

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="set_operation_mode", node_url=ALICE)))

    assert bot.drawn.calls == ["ask_for_params"]
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/node/operation_mode")]
    assert bot.drawn.initial == {"operation_mode": OperationMode.CORRELATION}


@pytest.mark.parametrize(
    ("status", "payload"),
    [(503, {"detail": "down for maintenance"}), (200, {})],
    ids=["refused", "wrong shape"],
)
def test_the_mode_form_falls_back_on_the_signature_default_when_it_cannot_read_a_mode(
    status: int, payload: object
) -> None:
    """A form opening on the default beats no form, so a Node that cannot be read must not block the modal.

    The default is entanglement, today's mode, so a Node that cannot be read leaves the
    form where an operator expects to find it (agent decision A6).
    """

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, json=payload)

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="set_operation_mode", node_url=ALICE)))

    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == {"operation_mode": OperationMode.ENTANGLEMENT}


@pytest.mark.parametrize(("asked_for", "answered"), [("correlation", "entanglement"), ("entanglement", "correlation")])
def test_changing_the_operation_mode_reports_what_the_node_answers(asked_for: str, answered: str) -> None:
    """Decision 8: the body is the mode's own string, and the report is the mode the Node answers.

    The Node is what plays the next Game, so what it says afterwards — not what was asked
    for — is the fact to report.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answered)

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(action="set_operation_mode", node_url=ALICE, params={"operation_mode": asked_for})
    asyncio.run(dispatched(bot, pending))

    request = next(r for r in seen if r.url.path == "/node/operation_mode")
    assert request.method == "PUT"
    assert json.loads(request.content) == asked_for
    result = only_report(bot)
    assert result.status is Status.OK
    assert result.title == "Operation mode — uiuc-public-left"
    assert result.summary == f"uiuc-public-left now plays its two-player Games in {answered} mode."
    assert result.sections[0].status is None
    assert result.sections[0].fields == [Field(name="Operation mode", value=answered)]
    assert result.notes == ["The mode is saved to the Node's config and applied without a restart."]


@pytest.mark.parametrize(
    ("status", "payload", "because"),
    [
        (503, {"detail": "the config file is read-only"}, "read-only"),
        (200, {}, "is not an Operation mode"),
    ],
    ids=["refused", "wrong shape"],
)
def test_a_node_that_cannot_take_the_mode_change_is_a_fail_that_says_why(
    status: int, payload: object, because: str
) -> None:
    """A refusal and a wrong-shape answer are both a FAIL that carries the Node's own reason."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, json=payload)

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(action="set_operation_mode", node_url=ALICE, params={"operation_mode": "correlation"})
    asyncio.run(dispatched(bot, pending))

    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.title == "Operation mode — uiuc-public-left"
    assert result.summary == "The Node did not answer."
    assert because in (result.sections[0].error or "")


def test_the_calibration_form_asks_for_every_angle_and_every_rate() -> None:
    """Decision 16: four angle floats and three rate floats behind the Change calibration button.

    Named as the Node's own models name them, so the prefill and the form agree without
    either naming the values a second time.
    """
    act = WhobotSpy.actions["set_calibration"]

    assert act.label == "Change calibration"
    assert [parameter.name for parameter in act.parameters] == [
        *PolarizationAngles.model_fields,
        *ReferenceRates.model_fields,
    ]


# Unlike any default, so a prefill test passing on defaults can never pass for reading the Node.
NODE_CALIBRATION = {
    "polarization_angles": {"h": 10.0, "v": 20.0, "d": 30.0, "a": 40.0},
    "reference_rates": {"vv": 2100.0, "vd": 1400.0, "dd": 700.0},
}
NODE_CALIBRATION_FORM: dict[str, object] = {
    "h": 10.0,
    "v": 20.0,
    "d": 30.0,
    "a": 40.0,
    "vv": 2100.0,
    "vd": 1400.0,
    "dd": 700.0,
}

# What the operator submits and what the Node answers the PUT with: different in every
# value, and fractional in every field on both sides — so the body test catches a form
# field that truncates what was typed (an int annotation where a float belongs), and the
# report test catches a report of what was asked for and any rounding on the way out.
NEW_CALIBRATION = {
    "polarization_angles": {"h": 1.25, "v": 2.5, "d": 3.75, "a": 4.25},
    "reference_rates": {"vv": 11.5, "vd": 12.25, "dd": 13.75},
}
NEW_CALIBRATION_FORM: dict[str, object] = {
    "h": 1.25,
    "v": 2.5,
    "d": 3.75,
    "a": 4.25,
    "vv": 11.5,
    "vd": 12.25,
    "dd": 13.75,
}
ANSWERED_CALIBRATION = {
    "polarization_angles": {"h": 0.25, "v": 22.25, "d": 44.75, "a": -11.05},
    "reference_rates": {"vv": 2999.25, "vd": 1499.75, "dd": 749.05},
}

# Every value at its default: still a whole calibration, and the Node refuses a partial one.
DEFAULT_CALIBRATION = {
    "polarization_angles": {"h": 0.0, "v": 45.0, "d": 22.5, "a": -22.5},
    "reference_rates": {"vv": 3000.0, "vd": 1500.0, "dd": 750.0},
}
DEFAULT_CALIBRATION_FORM: dict[str, object] = {
    "h": 0.0,
    "v": 45.0,
    "d": 22.5,
    "a": -22.5,
    "vv": 3000.0,
    "vd": 1500.0,
    "dd": 750.0,
}


def test_the_calibration_form_opens_on_the_nodes_calibration() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=NODE_CALIBRATION)

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="set_calibration", node_url=ALICE)))

    assert bot.drawn.calls == ["ask_for_params"]
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/node/calibration")]
    assert bot.drawn.initial == NODE_CALIBRATION_FORM


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (503, {"detail": "down for maintenance"}),
        (200, {"polarization_angles": {"h": 10.0}}),
    ],
    ids=["refused", "wrong shape"],
)
def test_the_calibration_form_falls_back_on_a_fresh_nodes_values(status: int, payload: object) -> None:
    """A prefill that cannot read the Node still opens the form, on the values a Node with no keys set plays with."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, json=payload)

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="set_calibration", node_url=ALICE)))

    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == DEFAULT_CALIBRATION_FORM


def test_a_calibration_whose_values_equal_their_defaults_is_still_sent_whole() -> None:
    """The Node refuses a partial calibration (agent decision A4), so no value may be left out of the body.

    A value equal to its default is still a value the Node must be told.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DEFAULT_CALIBRATION)

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(action="set_calibration", node_url=ALICE, params=DEFAULT_CALIBRATION_FORM)
    asyncio.run(dispatched(bot, pending))

    request = next(r for r in seen if r.url.path == "/node/calibration")
    assert request.method == "PUT"
    body = json.loads(request.content)
    assert set(body["polarization_angles"]) == {"h", "v", "d", "a"}
    assert set(body["reference_rates"]) == {"vv", "vd", "dd"}
    assert only_report(bot).status is Status.OK


def test_changing_the_calibration_reports_what_the_node_answers() -> None:
    """The report shows the values the Node stored, at the precision it stored them.

    The Node answers the PUT with what it now plays with, and that — not what was asked
    for — is what the operator needs to see.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANSWERED_CALIBRATION)

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(action="set_calibration", node_url=ALICE, params=NEW_CALIBRATION_FORM)
    asyncio.run(dispatched(bot, pending))

    request = next(r for r in seen if r.url.path == "/node/calibration")
    assert request.method == "PUT"
    assert json.loads(request.content) == NEW_CALIBRATION
    result = only_report(bot)
    assert result.status is Status.OK
    assert result.title == "Calibration — uiuc-public-left"
    sections = {section.label: section for section in result.sections}
    assert set(sections) == {"Calibrated angles", "Reference rates"}
    assert all(section.status is None for section in result.sections)
    assert all(field.status is None for section in result.sections for field in section.fields)
    assert {f.name: f.value for f in sections["Calibrated angles"].fields} == {
        "H": "0.25°",
        "V": "22.25°",
        "D": "44.75°",
        "A": "-11.05°",
    }
    assert {f.name: f.value for f in sections["Reference rates"].fields} == {
        "VV": "2999.25 coincidences/s",
        "VD": "1499.75 coincidences/s",
        "DD": "749.05 coincidences/s",
    }
    assert result.notes == ["The calibration is saved to the Node's config and applied without a restart."]


@pytest.mark.parametrize(
    ("status", "payload", "because"),
    [
        (422, {"detail": "polarization_angles.v: Field required"}, "Field required"),
        (200, {"polarization_angles": {"h": 10.0}}, "did not answer with a calibration"),
    ],
    ids=["refused", "wrong shape"],
)
def test_a_node_that_cannot_take_the_calibration_is_a_fail_that_says_why(
    status: int, payload: object, because: str
) -> None:
    """A refusal and a wrong-shape answer are both a FAIL that carries the Node's own reason."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, json=payload)

    bot = with_node_api(handler, ALICE)
    pending = PendingInvocation(action="set_calibration", node_url=ALICE, params=NEW_CALIBRATION_FORM)
    asyncio.run(dispatched(bot, pending))

    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.title == "Calibration — uiuc-public-left"
    assert result.summary == "The Node did not answer."
    assert because in (result.sections[0].error or "")


def test_every_call_the_two_actions_make_carries_the_configured_node_timeout() -> None:
    """The prefill and the submit of both Actions spend node_timeout_s, and nothing else."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/node/operation_mode":
            return httpx.Response(200, json="correlation")
        return httpx.Response(200, json=NODE_CALIBRATION)

    clicks = [
        PendingInvocation(action="set_operation_mode", node_url=ALICE),
        PendingInvocation(action="set_operation_mode", node_url=ALICE, params={"operation_mode": "correlation"}),
        PendingInvocation(action="set_calibration", node_url=ALICE),
        PendingInvocation(action="set_calibration", node_url=ALICE, params=NEW_CALIBRATION_FORM),
    ]
    for pending in clicks:
        asyncio.run(dispatched(with_node_api(handler, ALICE, node_timeout_s=7.5), pending))

    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/node/operation_mode"),
        ("PUT", "/node/operation_mode"),
        ("GET", "/node/calibration"),
        ("PUT", "/node/calibration"),
    ]
    assert all(request.extensions["timeout"] == httpx.Timeout(7.5).as_dict() for request in seen)


# --------------------------------------------------------------------------------------
# screenshot: the image, and the debug file that stands in for a display.
# --------------------------------------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"not really a PNG, but it starts like one" * 20
GIF = b"GIF89a" + b"nor is this a GIF" * 20


def serving_an_image(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
    return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})


def test_screenshot_carries_the_image_on_the_result() -> None:
    """``Report.image`` gets its first real exercise here; nothing else produces one."""
    bot = with_node_api(serving_an_image, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.OK
    assert result.image == PNG


def test_a_node_that_cannot_capture_says_why() -> None:
    """The Node's own reason must survive the trip, or the operator reads its logs to learn it."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, json={"detail": "'maim' is not installed on this Node"})

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.image is None
    assert result.sections[0].error is not None
    assert "maim" in result.sections[0].error


def test_something_that_is_not_an_image_is_not_uploaded_as_a_screenshot() -> None:
    """A captive portal answering 200 with HTML must not become a picture of a Node."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, text="<html>sign in to continue</html>")

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.image is None


def debug_bot(path: Path, *urls: str) -> ClientSpy:
    """Build a Whobot answering Screenshot from a file, whose Node API refuses every call."""

    def never(request: httpx.Request) -> httpx.Response:
        msg = f"the Node was called: {request.url}"
        raise AssertionError(msg)

    bot = ClientSpy(settings_for(*urls, debug_screenshot_path=path))
    bot.handler = never
    return bot


def test_the_debug_screenshot_answers_from_a_file_without_calling_the_node(tmp_path: Path) -> None:
    """The point of the setting: the whole Action path runs on a laptop that has no display."""
    image = tmp_path / "spongebob.gif"
    image.write_bytes(GIF)

    bot = debug_bot(image, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    assert only_report(bot).image == GIF


def test_the_debug_screenshot_says_it_is_not_the_nodes_display(tmp_path: Path) -> None:
    """A picture mistaken for a Node's display is worse than no picture at all."""
    image = tmp_path / "spongebob.gif"
    image.write_bytes(GIF)

    bot = debug_bot(image, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.WARN
    assert result.summary is not None
    assert "not the Node's display" in result.summary
    assert any("debug_screenshot_path" in note for note in result.notes)


def test_a_debug_screenshot_path_that_cannot_be_read_is_a_failure(tmp_path: Path) -> None:
    """Silently falling back to the Node would answer a laptop's screenshot with a 503 instead."""
    bot = debug_bot(tmp_path / "deleted.gif", ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.image is None
    assert result.summary is not None
    assert "debug_screenshot_path" in result.summary


# --------------------------------------------------------------------------------------
# reboot: the confirm step, and the poll that says whether the Node came back.
# --------------------------------------------------------------------------------------


@pytest.fixture
def _reboot_without_the_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the settle period and the poll interval, keeping the sequence intact."""
    monkeypatch.setattr("pqn_whobot.whobot.REBOOT_SETTLE_S", 0.0)
    monkeypatch.setattr("pqn_whobot.whobot.REBOOT_POLL_INTERVAL_S", 0.0)


def rebooting(comes_back_after: int | None) -> Callable[[httpx.Request], httpx.Response]:
    """Build a Node that acks a reboot, then refuses ``comes_back_after`` polls before answering.

    ``None`` is the Node that never comes back — the failure the poll exists to catch.
    """
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path == "/system/reboot":
            return httpx.Response(200, json={"scheduled": True, "detail": "Rebooting in 1s"})
        polls += 1
        if comes_back_after is None or polls <= comes_back_after:
            msg = "Connection refused"
            raise httpx.ConnectError(msg, request=request)
        return httpx.Response(200, json={"node_name": "uiuc-public-left"})

    return handler


def test_reboot_asks_to_confirm_first() -> None:
    """The first shipped destructive Action: picking it from a dropdown must not reboot anything."""
    bot = run(spy(ALICE), PendingInvocation(action="reboot", node_url=ALICE))
    assert bot.drawn.calls == ["ask_to_confirm"]


@pytest.mark.usefixtures("_reboot_without_the_waiting")
def test_a_confirmed_reboot_polls_until_the_node_answers() -> None:
    bot = with_node_api(rebooting(comes_back_after=2), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="reboot", node_url=ALICE, confirmed=True)))
    result = only_report(bot)
    assert result.status is Status.OK
    assert result.summary is not None
    assert "is back" in result.summary
    assert [f.status for f in result.sections[0].fields] == [Status.OK, Status.OK]


@pytest.mark.usefixtures("_reboot_without_the_waiting")
def test_a_node_that_never_comes_back_is_reported_as_still_down() -> None:
    """The real guardrail: the confirm step protects the wrong Node, this protects against a lost one."""
    bot = with_node_api(rebooting(comes_back_after=None), ALICE, reboot_wait_s=0.05)
    asyncio.run(dispatched(bot, PendingInvocation(action="reboot", node_url=ALICE, confirmed=True)))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.sections[0].fields[-1].status is Status.FAIL
    assert "still down" in result.sections[0].fields[-1].value


def test_a_node_that_refuses_the_reboot_is_not_polled_for() -> None:
    """Nothing was rebooted, so waiting five minutes to say so would be five wasted minutes."""

    def handler(request: httpx.Request) -> httpx.Response:
        msg = "Connection refused"
        raise httpx.ConnectError(msg, request=request)

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="reboot", node_url=ALICE, confirmed=True)))
    result = only_report(bot)
    assert result.status is Status.FAIL
    assert result.summary is not None
    assert "nothing was rebooted" in result.summary


def test_an_answer_that_does_not_acknowledge_a_reboot_is_refused() -> None:
    """Something else on that port must not be read as a Node that is now rebooting."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"hello": "world"})

    bot = with_node_api(handler, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="reboot", node_url=ALICE, confirmed=True)))
    assert only_report(bot).status is Status.FAIL


# --------------------------------------------------------------------------------------
# Neutrality: nothing an Action produces may carry platform markup.
# --------------------------------------------------------------------------------------


SHORTCODE = re.compile(r":[a-z0-9_+-]+:")


def _section_strings(sections: list[Section]) -> list[str]:
    strings = []
    for section in sections:
        strings += [text for text in (section.label, section.note) if text]
        strings += [f.name for f in section.fields]
        strings += [f.value for f in section.fields]
    return strings


def _human_strings(result: ActionResult) -> list[str]:
    """Every string an operator reads, except ``Section.error``, which may hold a traceback.

    Takes an ``ActionResult`` and walks whichever shape it is, so the rule stays stated in one
    place however many result shapes the package grows. It asserts on a shape it has no walker
    for, which is what stops a third result type quietly escaping the rule.
    """
    if isinstance(result, Report):
        strings = [result.title, *([result.summary] if result.summary else []), *result.notes]
        return strings + _section_strings(result.sections)
    if isinstance(result, DigestResult):
        strings = [result.title, *([result.summary] if result.summary else []), *result.notes]
        for node in result.nodes:
            strings += [node.name, node.api_url, *_section_strings(node.sections)]
        return strings
    msg = f"no walker for {type(result).__name__}, so the no-markup rule would silently skip it"
    raise AssertionError(msg)


def assert_no_markup(result: ActionResult) -> None:
    for text in _human_strings(result):
        assert "*" not in text, text
        assert "`" not in text, text
        assert not SHORTCODE.search(text), text


@pytest.mark.parametrize(
    "pending",
    [
        PendingInvocation(action="list_nodes"),
        PendingInvocation(action="quiet"),
        PendingInvocation(action="raises"),
        PendingInvocation(action="slow"),
        PendingInvocation(action="needs_form", params={"loud": True}),
    ],
)
def test_no_action_output_contains_platform_markup(pending: PendingInvocation) -> None:
    """An Action describes what happened; decoration is the Chat Platform's business."""
    assert_no_markup(only_report(run(spy(ALICE, BOB), pending)))


def test_an_angle_sweeps_output_carries_no_platform_markup() -> None:
    bot = with_node_api(node_api(), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="angle_sweep", node_url=ALICE, params=SWEEP_FORM)))
    assert_no_markup(only_report(bot))


def test_a_check_ups_output_carries_no_platform_markup() -> None:
    """The result with the most strings in it, and the only one with two levels of them."""
    assert_no_markup(checked(with_node_api(node_api(), ALICE)))


def test_the_rule_refuses_a_result_shape_it_cannot_walk() -> None:
    """Otherwise a third result type would pass every neutrality test by not being read."""

    class Bespoke(ActionResult):
        pass

    with pytest.raises(AssertionError, match="no walker"):
        assert_no_markup(Bespoke())


@pytest.mark.usefixtures("_reboot_without_the_waiting")
def test_the_reboot_report_carries_no_platform_markup() -> None:
    """The Action most tempted by decoration — a Node's address in backticks — must resist it."""
    bot = with_node_api(rebooting(comes_back_after=0), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="reboot", node_url=ALICE, confirmed=True)))
    assert_no_markup(only_report(bot))


def test_the_debug_screenshot_report_carries_no_platform_markup(tmp_path: Path) -> None:
    """It names a filesystem path, which is where a backtick would feel most natural."""
    image = tmp_path / "spongebob.gif"
    image.write_bytes(GIF)

    bot = debug_bot(image, ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="screenshot", node_url=ALICE)))
    assert_no_markup(only_report(bot))


# --------------------------------------------------------------------------------------
# Checking a Node over: the Games, the hardware checklist, and the whole check-up that the
# Daily Digest fans out. Driven through the Actions, because that is how they are reached.
# --------------------------------------------------------------------------------------

HEALTHY = {
    "router": {"reachable": True, "latency_ms": 3.2},
    "devices": [{"reachable": True, "provider": "prov", "name": "tagger", "purpose": "counting", "latency_ms": 8.0}],
    "rotary_encoder": None,
    "follower_node": {"reachable": True, "latency_ms": 12.0},
}

CHSH = {
    "chsh_value": 2.6134919,
    "chsh_error": 0.04,
    "expectation_values": [0.7, -0.65],
    "expectation_errors": [0.01, 0.01],
    "expectation_values_sign_fixed": [0.7, 0.65],
}

SWEEP_FORM: dict[str, object] = {
    "follower_polarization": "D",
    "custom_angle": 0.0,
    "step_deg": 5.0,
    "integration_time_s": 2.0,
}
SWEEP = {
    "leader_name": "alice",
    "follower_name": "bob",
    "follower_angle": 0.0,
    "step_deg": 90.0,
    "integration_time_s": 2.0,
    "angles": [0.0, 90.0, 180.0, 270.0],
    "coincidences": [100, 10, 100, 10],
}

ALL_GAMES_ON = {"chsh": True, "qf": True, "ssm": True}


def node_api(  # noqa: PLR0913 - one keyword per endpoint a check-up can hit, so a test sets one
    *,
    availability: object = ALL_GAMES_ON,
    refuse: dict[str, int] | None = None,
    seen: list[httpx.Request] | None = None,
    mode: str = "entanglement",
    follower: str | None = None,
    follower_mode: str = "correlation",
    follower_refuses: bool = False,
    unattended: object = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer every endpoint a check-up calls, with any of them able to refuse instead.

    ``follower`` is the address ``/node/config`` reports, and is ``None`` by default — no
    follower configured, so the digest reads no follower mode. A request whose host is that
    follower is answered with ``follower_mode``, so a mismatch can be set up, or refused by
    connection when ``follower_refuses`` is set, for a follower that cannot be read. ``unattended``
    is the SSM game's answer; the default is a clean entanglement-mode game over eight questions.
    """
    refusals = refuse or {}
    answers: dict[str, object] = {
        "/health/": HEALTHY,
        "/games/availability": availability,
        "/chsh/": CHSH,
        "/chsh/sweep": SWEEP,
        "/rng/fortune": [42, 137],
        "/node/operation_mode": mode,
        "/node/config": {"node_name": "uiuc-public-left", "follower_node_address": follower},
        "/qkd/unattended": unattended if unattended is not None else entangled([0] * 8, [0] * 8),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        is_follower = follower is not None and request.url.host == follower
        if is_follower and follower_refuses:
            msg = f"{follower} is not answering"
            raise httpx.ConnectError(msg, request=request)
        if is_follower and path == "/node/operation_mode":
            return httpx.Response(200, json=follower_mode)
        if path in refusals:
            return httpx.Response(refusals[path], json={"detail": f"{path} is not having it"})
        if path not in answers:
            return httpx.Response(404)
        return httpx.Response(200, json=answers[path])

    return handler


def checked(bot: ClientSpy, action_name: str = "check_node", **params: object) -> DigestResult:
    """Run one check-up Action and return the digest it posted."""
    act = bot.actions[action_name]
    node_url = ALICE if act.scope is Scope.ONE else None
    pending = PendingInvocation(action=action_name, node_url=node_url, params=params or None)
    asyncio.run(dispatched(bot, pending))
    assert len(bot.drawn.results) == 1
    result = bot.drawn.results[0]
    assert isinstance(result, DigestResult)
    return result


def sections_of(digest: DigestResult) -> dict[str, Section]:
    assert len(digest.nodes) == 1
    return {section.label or "": section for section in digest.nodes[0].sections}


# --- the pure helpers, which need no Node at all ---


def test_every_probed_thing_gets_its_own_row() -> None:
    """A Node with one dead device among nine live ones has to say which one is dead."""
    unhealthy = HealthStatus.model_validate(
        {
            **HEALTHY,
            "devices": [
                {"reachable": True, "provider": "prov", "name": "tagger", "purpose": "counting"},
                {"reachable": False, "provider": "prov", "name": "hwp", "purpose": "rotating", "error": "timed out"},
            ],
        }
    )

    hardware = _hardware_section(unhealthy)
    rows = {f.name: f for f in hardware.fields}

    assert hardware.status is Status.FAIL
    assert rows["prov/tagger (counting)"].status is Status.OK
    assert rows["prov/hwp (rotating)"].status is Status.FAIL
    assert "timed out" in rows["prov/hwp (rotating)"].value


def test_a_virtual_rotary_encoder_is_skipped_not_failed() -> None:
    """Nothing was probed, so it is neither working nor broken, and a skip is not bad news."""
    hardware = _hardware_section(HealthStatus.model_validate(HEALTHY))

    assert next(f for f in hardware.fields if f.name == "Rotary encoder").status is Status.SKIPPED
    assert hardware.status is Status.OK


def test_a_violated_bell_inequality_is_the_good_news() -> None:
    verdict = _bell_verdict(2.61)

    assert verdict.status is Status.OK
    assert "violated" in verdict.value


def test_an_unviolated_bell_inequality_warns_rather_than_fails() -> None:
    """The hardware answered and the Game ran; what stopped is the demonstration itself."""
    verdict = _bell_verdict(1.94)

    assert verdict.status is Status.WARN
    assert "not violated" in verdict.value


# --- one Node's whole check-up ---


def test_a_healthy_node_reports_its_hardware_its_mode_and_every_game() -> None:
    digest = checked(with_node_api(node_api(), ALICE))

    assert digest.status is Status.OK
    # The literal heading is named, so a rename of the section is a failed test rather than a
    # silently different report for the operator.
    assert list(sections_of(digest)) == ["Hardware", "Operation mode", *GAME_TITLES.values()]
    chsh = sections_of(digest)[GAME_TITLES["chsh"]]
    assert next(f.value for f in chsh.fields if f.name == "S") == "2.6135 ± 0.0400"
    assert chsh.note is not None, "a Game reports how long it took"


def test_the_ssm_section_is_played_when_the_node_offers_it() -> None:
    """An unattended run can play SSM now (decision 10), so the digest does."""
    ssm = sections_of(checked(with_node_api(node_api(), ALICE)))[GAME_TITLES["ssm"]]

    assert ssm.status is Status.OK
    assert {field.name: field.value for field in ssm.fields}["Operation mode"] == "entanglement"


def test_an_ssm_section_the_node_does_not_offer_is_skipped_like_the_other_games() -> None:
    """A Game switched off on purpose is skipped, with the same wording as CHSH and QF."""
    digest = checked(with_node_api(node_api(availability={"chsh": True, "qf": True, "ssm": False}), ALICE))

    ssm = sections_of(digest)[GAME_TITLES["ssm"]]
    assert ssm.status is Status.SKIPPED
    assert ssm.fields[0].value == "Not available on this Node."
    assert digest.status is Status.OK, "a Game switched off on purpose is not bad news"


# --- the Operation mode section: each Node's mode, and its follower's (decision 9, A13) ---


def test_every_node_reports_its_operation_mode() -> None:
    """The mode is a Node-level fact, reported whatever Games it offers and in both modes."""
    entanglement = sections_of(checked(with_node_api(node_api(mode="entanglement"), ALICE)))
    correlation = sections_of(checked(with_node_api(node_api(mode="correlation"), ALICE)))

    assert {field.name: field.value for field in entanglement[OPERATION_MODE_LABEL].fields}["Operation mode"] == (
        "entanglement"
    )
    assert {field.name: field.value for field in correlation[OPERATION_MODE_LABEL].fields}["Operation mode"] == (
        "correlation"
    )


def test_the_mode_is_reported_even_when_the_node_offers_no_games() -> None:
    """Decision 9: the mode changes what the GUI tells CHSH players too, so it is always read."""
    seen: list[httpx.Request] = []
    digest = checked(with_node_api(node_api(availability={"chsh": False, "qf": False, "ssm": False}, seen=seen), ALICE))

    assert sections_of(digest)[OPERATION_MODE_LABEL].status is Status.OK
    assert "/node/operation_mode" in [request.url.path for request in seen]


def test_the_mode_read_is_reused_by_the_ssm_section() -> None:
    """The mode is read once per Node, and the SSM section is handed what was already read."""
    seen: list[httpx.Request] = []
    checked(with_node_api(node_api(seen=seen), ALICE))

    reads = [request for request in seen if request.url.path == "/node/operation_mode"]
    assert len(reads) == 1, "the mode is read once and reused for the SSM report"


def test_a_node_whose_own_mode_cannot_be_read_warns_and_the_ssm_section_fails_on_its_own() -> None:
    """A mode read that fails is a WARN on the mode section; the SSM section then fails too.

    The SSM section needs the mode to pick its default Answers, so with no Answers given it
    reports the failure rather than playing. Its mode row says "unknown".
    """
    digest = checked(with_node_api(node_api(refuse={"/node/operation_mode": 503}), ALICE))

    mode_section = sections_of(digest)[OPERATION_MODE_LABEL]
    assert mode_section.status is Status.WARN
    assert mode_section.error is not None
    # The row still says which fact is unknown, so the section reads as a report rather than a blank.
    mode_row = next(field for field in mode_section.fields if field.name == "Operation mode")
    assert mode_row.value == "unknown"
    assert mode_row.status is Status.WARN
    ssm = sections_of(digest)[GAME_TITLES["ssm"]]
    assert ssm.status is Status.FAIL
    assert {field.name: field.value for field in ssm.fields}["Operation mode"] == "unknown"


def test_a_config_read_that_fails_warns_the_follower_row_and_asks_no_follower() -> None:
    """When this Node's own config cannot be read, the follower is unknown and never asked."""
    seen: list[httpx.Request] = []
    digest = checked(with_node_api(node_api(refuse={"/node/config": 503}, seen=seen), ALICE, node_timeout_s=0.5))

    follower = next(
        field for field in sections_of(digest)[OPERATION_MODE_LABEL].fields if field.name == "Follower mode"
    )
    assert follower.status is Status.WARN
    assert follower.value.startswith("unknown")
    assert "config" in follower.value
    assert {request.url.host for request in seen} == {"node-a.invalid"}, "only this Node was asked"


def test_a_matching_follower_mode_is_shown_without_a_warning() -> None:
    """Decision 9: only a *difference* is a mistake worth flagging.

    The follower is asked at ``http://<address>/node/operation_mode`` (A13): Nodes serve plain
    HTTP, so an https request would never be answered and every follower would read as unknown.
    """
    seen: list[httpx.Request] = []
    digest = checked(with_node_api(node_api(mode="correlation", follower="10.0.0.9", seen=seen), ALICE))

    section = sections_of(digest)[OPERATION_MODE_LABEL]
    follower = next(field for field in section.fields if field.name == "Follower mode")
    assert follower.status is Status.OK
    assert follower.value == "correlation"
    assert section.status is Status.OK
    follower_request = next(request for request in seen if request.url.host == "10.0.0.9")
    assert str(follower_request.url) == "http://10.0.0.9/node/operation_mode"


def test_a_follower_in_a_different_mode_warns_and_names_both() -> None:
    """A mismatch is an operator mistake; the flag names both modes so it can be fixed."""
    digest = checked(with_node_api(node_api(mode="entanglement", follower="10.0.0.9"), ALICE, node_timeout_s=0.5))

    section = sections_of(digest)[OPERATION_MODE_LABEL]
    follower = next(field for field in section.fields if field.name == "Follower mode")
    assert follower.status is Status.WARN
    assert "correlation" in follower.value
    assert "entanglement" in follower.value
    assert section.status is Status.WARN


def test_a_follower_mode_that_cannot_be_read_shows_unknown_and_warns() -> None:
    """A13: an unreadable follower must WARN, so a mismatch is never hidden."""
    digest = checked(with_node_api(node_api(follower="down.invalid", follower_refuses=True), ALICE, node_timeout_s=0.5))

    section = sections_of(digest)[OPERATION_MODE_LABEL]
    follower = next(field for field in section.fields if field.name == "Follower mode")
    assert follower.status is Status.WARN
    assert follower.value.startswith("unknown")
    assert section.status is Status.WARN


def test_no_configured_follower_is_said_without_a_warning() -> None:
    """A13: no follower configured means no flag — and no follower mode is even asked for."""
    seen: list[httpx.Request] = []
    digest = checked(with_node_api(node_api(follower=None, seen=seen), ALICE))

    section = sections_of(digest)[OPERATION_MODE_LABEL]
    follower = next(field for field in section.fields if field.name == "Follower mode")
    assert follower.status is Status.OK
    assert follower.value == "none configured"
    assert section.status is Status.OK
    assert [request.url.path for request in seen].count("/node/config") == 1


def test_an_empty_follower_address_is_treated_as_none_configured() -> None:
    """A13: an empty address is a Node with no follower — not one at an empty host.

    Otherwise the row would WARN "unknown at :", and Whobot would try to open a connection to a
    host that does not exist.
    """
    seen: list[httpx.Request] = []
    digest = checked(with_node_api(node_api(follower="", seen=seen), ALICE))

    section = sections_of(digest)[OPERATION_MODE_LABEL]
    follower = next(field for field in section.fields if field.name == "Follower mode")
    assert follower.status is Status.OK
    assert follower.value == "none configured"
    assert section.status is Status.OK
    assert {request.url.host for request in seen} == {"node-a.invalid"}, "no empty-host follower was asked"


def test_the_digests_ssm_check_follows_the_nodes_mode() -> None:
    """Decision 13: the digest's SSM check is the mode's — correlation reports rates vs references."""
    seen: list[httpx.Request] = []
    digest = checked(
        with_node_api(
            node_api(
                mode="correlation",
                unattended=correlated([3000.0, 1500.0, 1500.0, 750.0], ["high", "medium", "medium", "low"]),
                seen=seen,
            ),
            ALICE,
            ssm_questions=4,
        )
    )

    ssm = sections_of(digest)[GAME_TITLES["ssm"]]
    assert {field.name: field.value for field in ssm.fields}["Operation mode"] == "correlation"
    assert ssm.status is Status.OK
    assert "/node/calibration" in [request.url.path for request in seen], "correlation shows rates vs references"


def test_the_digest_ssm_game_uses_the_configured_question_count_and_no_integration_time() -> None:
    """The digest plays ``ssm_questions`` questions and sends no integration time (decision 13, A9).

    A non-default count is used so a hard-coded number would fail, and A9's "no time" is pinned by
    the absence of ``integration_time_s``: the Node then measures as its GUI game does.
    """
    seen: list[httpx.Request] = []
    checked(
        with_node_api(
            node_api(unattended=entangled([0] * 6, [0] * 6), seen=seen),
            ALICE,
            ssm_questions=6,
        )
    )

    game = next(request for request in seen if request.url.path == "/qkd/unattended")
    body = json.loads(game.content)
    assert body == {"leader_answers": ["a"] * 3 + ["b"] * 3, "follower_answers": ["a"] * 3 + ["b"] * 3}
    assert "integration_time_s" not in game.url.params


def test_the_digests_ssm_status_follows_the_configured_max_error_rate() -> None:
    """Decision 13: the SSM section WARNs when a basis's error rate is above ``ssm_max_error_rate``.

    A non-default threshold is used so a hard-coded 0.15 would fail: the bits disagree on every
    compared question, so the rate is 1.0 and the check warns against the configured 0.25.
    """
    digest = checked(
        with_node_api(
            node_api(unattended=entangled([0, 0, 0, 0], [1, 1, 1, 1])), ALICE, ssm_questions=4, ssm_max_error_rate=0.25
        )
    )

    ssm = sections_of(digest)[GAME_TITLES["ssm"]]
    assert ssm.status is Status.WARN
    fields = {field.name: field for field in ssm.fields}
    assert fields["Max error rate"].value == "0.25"


def test_a_failed_hardware_probe_does_not_stop_the_games() -> None:
    """The Games are what prove the physics still works, so they are still worth attempting."""
    digest = checked(with_node_api(node_api(refuse={"/health/": 500}), ALICE))

    assert digest.status is Status.FAIL
    assert sections_of(digest)["Hardware"].error is not None
    assert sections_of(digest)[GAME_TITLES["chsh"]].status is Status.OK


def test_a_failed_game_carries_the_nodes_own_reason() -> None:
    digest = checked(with_node_api(node_api(refuse={"/chsh/": 503}), ALICE))

    chsh = sections_of(digest)[GAME_TITLES["chsh"]]
    assert chsh.status is Status.FAIL
    assert "is not having it" in str(chsh.error)
    assert chsh.note is not None, "how long it ran before failing is worth knowing"
    assert sections_of(digest)[GAME_TITLES["qf"]].status is Status.OK, "one Game failing does not stop the next"


def test_a_game_the_node_does_not_offer_is_skipped_rather_than_attempted() -> None:
    digest = checked(with_node_api(node_api(availability={"chsh": False, "qf": True, "ssm": True}), ALICE))

    assert sections_of(digest)[GAME_TITLES["chsh"]].status is Status.SKIPPED
    assert digest.status is Status.OK, "a Game switched off on purpose is not bad news"


def test_availability_that_cannot_be_read_skips_every_game() -> None:
    """Whether a Game may run is the Node's answer to give.

    Assuming all-enabled is how an unattended run plays a Game an operator deliberately
    switched off, so the error is reported on each Game instead.
    """
    digest = checked(with_node_api(node_api(refuse={"/games/availability": 500}), ALICE))

    for title in GAME_TITLES.values():
        skipped = sections_of(digest)[title]
        assert skipped.status is Status.SKIPPED
    assert "could not read" in sections_of(digest)[GAME_TITLES["chsh"]].fields[0].value


def test_an_unreachable_node_is_one_section_and_no_calls() -> None:
    """Probe nothing on a Node the registry already reported as unreachable.

    Spending both Games' timeouts to rediscover it would make one unplugged Node the slowest
    part of the whole digest.
    """
    seen: list[httpx.Request] = []
    bot = with_node_api(node_api(seen=seen), ALICE)
    FLEET[:] = [Node(api_url=ALICE, reachable=False, error="ConnectError: refused")]

    digest = checked(bot)

    assert seen == []
    assert digest.status is Status.FAIL
    assert len(digest.nodes[0].sections) == 1
    assert "refused" in str(digest.nodes[0].sections[0].error)


def test_the_configured_measurement_values_are_what_an_unattended_run_uses() -> None:
    """`timetagger_address` and `basis` come from Whobot's config, not from the Node."""
    seen: list[httpx.Request] = []
    bot = with_node_api(node_api(seen=seen), ALICE, timetagger_address="10.0.0.5:9000", basis=(11.0, 33.5))

    checked(bot)

    chsh = next(r for r in seen if r.url.path == "/chsh/")
    fortune = next(r for r in seen if r.url.path == "/rng/fortune")
    assert chsh.url.params["timetagger_address"] == "10.0.0.5:9000"
    assert fortune.url.params["timetagger_address"] == "10.0.0.5:9000"
    assert json.loads(chsh.content) == [11.0, 33.5]


# --- the two Game Actions an operator drives by hand ---


def test_running_chsh_by_hand_measures_at_the_angles_asked_for() -> None:
    """The whole reason the Action takes parameters: an interactive run is aimed at something."""
    seen: list[httpx.Request] = []
    bot = with_node_api(node_api(seen=seen), ALICE, basis=(0.0, 22.5))

    pending = PendingInvocation(action="run_chsh", node_url=ALICE, params={"angle_a": 15.0, "angle_b": 60.0})
    asyncio.run(dispatched(bot, pending))

    assert json.loads(next(r for r in seen if r.url.path == "/chsh/").content) == [15.0, 60.0]
    assert only_report(bot).status is Status.OK


def test_the_chsh_form_opens_on_the_angles_an_unattended_run_would_use() -> None:
    """So the operator sees what the digest measures, and changes it deliberately."""
    bot = with_node_api(node_api(), ALICE, basis=(11.0, 33.5))

    asyncio.run(dispatched(bot, PendingInvocation(action="run_chsh", node_url=ALICE)))

    assert bot.drawn.initial == {"angle_a": 11.0, "angle_b": 33.5}


def test_running_a_fortune_by_hand_reports_what_each_channel_drew() -> None:
    bot = with_node_api(node_api(), ALICE)

    asyncio.run(dispatched(bot, PendingInvocation(action="run_fortune", node_url=ALICE)))

    result = only_report(bot)
    assert result.status is Status.OK
    assert result.sections[0].fields[0].value == "42, 137"


# --------------------------------------------------------------------------------------
# What an Action is allowed to take, which is worked out from the configuration it spends
# rather than fixed when the class is created.
# --------------------------------------------------------------------------------------


def test_a_declared_number_is_what_the_action_gets() -> None:
    """Most Actions want a plain bound, and those keep working unchanged."""
    act = FlowSpy.actions["screenshot"]

    assert act.timeout_for(settings_for()) == pytest.approx(60.0)


def test_a_budget_follows_the_configuration_it_spends() -> None:
    """Raise the wait and the Reboot Action is allowed longer, with nothing else to edit.

    This is what replaced the validator: the Action's bound *is* the sum of the calls it makes,
    so a config edit cannot leave it too small to report what it found.
    """
    act = FlowSpy.actions["reboot"]
    patient = settings_for(reboot_wait_s=1800.0)

    assert act.timeout_for(settings_for()) == pytest.approx(30.0 + 300.0 + 5.0)
    assert act.timeout_for(patient) == pytest.approx(30.0 + 1800.0 + 5.0)


def test_a_game_action_is_allowed_the_game_plus_the_call_that_starts_it() -> None:
    generous = settings_for(per_game_timeout_s=900.0)

    assert FlowSpy.actions["run_chsh"].timeout_for(generous) == pytest.approx(930.0)
    assert one_game_budget(generous) == pytest.approx(930.0)


def test_checking_a_node_is_allowed_every_call_and_game_it_makes() -> None:
    """The disagreement this deletes: 900s of budget for 1200s of Games, chosen by nobody.

    The budget is the sum of what a Node's check-up spends: six reads (hardware, availability,
    Operation mode, follower address, the follower's mode and a correlation-mode calibration)
    and three Games (CHSH, Quantum Fortune and SSM).
    """
    settings = settings_for()

    assert one_node_budget(settings) == pytest.approx(6 * 30.0 + 3 * 600.0)
    assert FlowSpy.actions["check_node"].timeout_for(settings) == pytest.approx(one_node_budget(settings))
    assert one_node_budget(settings) >= 3 * settings.per_game_timeout_s, "a Node's Games must fit its budget"


def test_a_budget_that_cannot_be_worked_out_falls_back_rather_than_refusing_to_run() -> None:
    """An announcement must always be followed by a result, including when the arithmetic is wrong."""

    def broken(_settings: WhobotSettings) -> float:
        raise ZeroDivisionError

    act = replace(FlowSpy.actions["screenshot"], timeout_s=broken)

    assert act.timeout_for(settings_for()) == pytest.approx(DEFAULT_TIMEOUT_S)


# --------------------------------------------------------------------------------------
# The whole-fleet digest.
# --------------------------------------------------------------------------------------


def test_the_digest_reports_every_registered_node() -> None:
    digest = checked(with_node_api(node_api(), ALICE, BOB), "run_digest")

    assert [node.api_url for node in digest.nodes] == [ALICE, BOB]
    assert digest.summary == "2 of 2 Nodes reported no problems"
    assert digest.status is Status.OK


def test_nodes_are_checked_one_at_a_time() -> None:
    """A requirement, not simplicity: in two-Node CHSH one Node is another's follower.

    Concurrent runs would contend for the same follower and the same timetagger, so the numbers
    would be worthless. One Node's check-up must therefore *finish* before the next one starts.

    Instrumented around ``_probe_node`` rather than around the HTTP calls, because the mock
    transport answers without yielding to the event loop — so a digest rewritten as
    ``asyncio.gather`` would still produce perfectly grouped requests and a test watching those
    would pass. The ``sleep(0)`` is what makes the difference observable: under ``gather`` the
    order becomes start, start, end, end.
    """

    class Recording(ClientSpy):
        order: ClassVar[list[str]] = []

        async def _probe_node(self, node: Node) -> NodeDigest:
            self.order.append(f"start {node.name}")
            await asyncio.sleep(0)
            digest = await super()._probe_node(node)
            self.order.append(f"end {node.name}")
            return digest

    bot = Recording(settings_for(ALICE, BOB))
    bot.handler = node_api()

    checked(bot, "run_digest")

    assert Recording.order == [
        "start uiuc-public-left",
        "end uiuc-public-left",
        "start ufl-public-right",
        "end ufl-public-right",
    ]


def test_one_unreachable_node_does_not_stop_the_others() -> None:
    """The whole reason a failure is a section rather than an exception."""
    bot = with_node_api(node_api(), ALICE, BOB)
    FLEET[:] = [Node(api_url=ALICE, reachable=False, error="ConnectError: refused"), REACHABLE[1]]

    digest = checked(bot, "run_digest")

    assert [node.status for node in digest.nodes] == [Status.FAIL, Status.OK]
    assert digest.summary == "1 of 2 Nodes reported no problems"
    assert digest.status is Status.FAIL


def test_a_node_that_outlasts_its_budget_is_one_section_and_the_run_continues() -> None:
    """One machine must not be able to spend the whole fleet's time."""

    class SlowFirstNode(ClientSpy):
        async def _probe_node(self, node: Node) -> NodeDigest:
            if node.api_url == ALICE:
                await asyncio.sleep(10)
            return await super()._probe_node(node)

    bot = SlowFirstNode(settings_for(ALICE, BOB, node_timeout_s=0.001, per_game_timeout_s=0.001))
    bot.handler = node_api()

    digest = checked(bot, "run_digest")

    assert digest.nodes[0].status is Status.FAIL
    assert "did not finish" in str(digest.nodes[0].sections[0].fields[0].value)
    assert digest.nodes[1].status is Status.OK, "the next Node is still checked"


def test_a_digest_with_no_nodes_says_so_rather_than_reporting_nothing() -> None:
    bot = ClientSpy(settings_for())
    bot.handler = node_api()

    digest = checked(bot, "run_digest")

    assert digest.status is Status.WARN
    assert digest.nodes == []
    assert "No Nodes are registered" in str(digest.summary)


def test_the_digests_budget_grows_with_the_registry() -> None:
    """What the callable timeout is for: adding a Node must not need a constant revisited."""
    act = FlowSpy.actions["run_digest"]

    two = act.timeout_for(settings_for(ALICE, BOB))
    four = act.timeout_for(settings_for(ALICE, BOB, "http://c.invalid:9000", "http://d.invalid:9000"))

    assert two == pytest.approx(2 * one_node_budget(settings_for()) + 30.0)
    assert four - two == pytest.approx(2 * one_node_budget(settings_for()))


def test_the_digests_output_carries_no_platform_markup() -> None:
    assert_no_markup(checked(with_node_api(node_api(), ALICE, BOB), "run_digest"))


# --------------------------------------------------------------------------------------
# The scheduler. The clock is faked and the tick shortened, so nothing here waits on a
# real one. No Node is registered either: the digest's *content* is tested above, and an
# empty registry answers without touching the network.
# --------------------------------------------------------------------------------------

CHICAGO = ZoneInfo("America/Chicago")
SETTLE_S = 0.05
"""Long enough for many ticks at the shortened interval below."""


@dataclass
class FakeClock:
    """A clock the test moves by hand, standing in for the `datetime` the loop reads."""

    at: datetime

    def now(self, _tz: object = None) -> datetime:
        return self.at


def scheduled_bot(clock: FakeClock, monkeypatch: pytest.MonkeyPatch, **overrides: object) -> FlowSpy:
    monkeypatch.setattr("pqn_whobot.whobot.datetime", clock)
    monkeypatch.setattr("pqn_whobot.whobot.SCHEDULER_TICK_S", 0.005)
    return FlowSpy(settings_for(digest_channel="C0DIGEST", **overrides))


async def settle() -> None:
    await asyncio.sleep(SETTLE_S)


def test_a_scheduled_digest_posts_one_message_and_never_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ack to reply under: nobody clicked, so there is nobody waiting to read one."""
    bot = scheduled_bot(FakeClock(at=datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO)), monkeypatch)

    asyncio.run(bot._run_scheduled_digest())  # noqa: SLF001 - the loop's one step, without the waiting

    assert bot.drawn.calls == ["post_result"]
    assert bot.drawn.replies == [SCHEDULED]


def test_a_scheduled_digest_records_what_it_did(monkeypatch: pytest.MonkeyPatch) -> None:
    ran_at = datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO)
    bot = scheduled_bot(FakeClock(at=ran_at), monkeypatch)

    asyncio.run(bot._run_scheduled_digest())  # noqa: SLF001

    assert bot.settings.last_run_at == ran_at
    reloaded = WhobotSettings()
    assert reloaded.last_run_at == ran_at
    assert reloaded.last_result is not None
    assert reloaded.last_result.startswith("warn")


def test_a_missed_run_is_not_replayed_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whobot starting at 08:00 does not run the 07:00 digest it was down for."""
    clock = FakeClock(at=datetime(2026, 7, 29, 8, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7, last_run_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC))

    async def go() -> None:
        bot.start_scheduler()
        await settle()
        await bot.shutdown(grace_s=1.0)

    asyncio.run(go())

    assert bot.drawn.calls == []


def test_a_run_fires_when_its_time_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(at=datetime(2026, 7, 29, 6, 59, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7)

    async def go() -> None:
        bot.start_scheduler()
        await settle()
        assert bot.drawn.calls == []  # not yet due
        clock.at = datetime(2026, 7, 29, 7, 0, 1, tzinfo=CHICAGO)
        await settle()
        await bot.shutdown(grace_s=1.0)

    asyncio.run(go())

    # Once, and once only: after firing, the next target is tomorrow.
    assert bot.drawn.calls == ["post_result"]


def test_a_changed_schedule_rearms_without_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(at=datetime(2026, 7, 29, 6, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7)

    async def go() -> None:
        bot.start_scheduler()
        await settle()

        update_config(bot.settings, {"schedule_hour": 6, "schedule_minute": 30})
        await settle()  # the loop notices and re-arms for 06:30 while it is still 06:00

        clock.at = datetime(2026, 7, 29, 6, 31, tzinfo=CHICAGO)
        await settle()
        await bot.shutdown(grace_s=1.0)

    asyncio.run(go())

    # Nothing fires at 06:31 unless the running loop moved its target off 07:00.
    assert bot.drawn.calls == ["post_result"]


def test_a_digest_that_cannot_be_posted_does_not_stop_tomorrows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one thing that must not happen quietly: the loop dying and no digest ever again."""
    clock = FakeClock(at=datetime(2026, 7, 29, 6, 59, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7)
    attempts = 0

    async def refuse(*_: object) -> None:
        nonlocal attempts
        attempts += 1
        msg = "Slack said no"
        raise RuntimeError(msg)

    monkeypatch.setattr(bot, "post_result", refuse)

    async def go() -> None:
        bot.start_scheduler()
        await settle()  # let it arm for 07:00 today; a spawned task runs nothing until awaited
        clock.at = datetime(2026, 7, 29, 7, 0, 1, tzinfo=CHICAGO)
        await settle()
        clock.at = datetime(2026, 7, 30, 7, 0, 1, tzinfo=CHICAGO)
        await settle()
        await bot.shutdown(grace_s=1.0)

    asyncio.run(go())

    assert attempts == 2  # noqa: PLR2004 - today's and tomorrow's, so the loop outlived the failure
    # And a digest nobody could be told about is not recorded, so it still reads as missed.
    assert WhobotSettings().last_run_at is None


def test_shutdown_stops_the_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(at=datetime(2026, 7, 29, 6, 59, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7)

    async def go() -> None:
        bot.start_scheduler()
        await settle()
        await bot.shutdown(grace_s=1.0)
        clock.at = datetime(2026, 7, 29, 7, 0, 1, tzinfo=CHICAGO)
        await settle()

    asyncio.run(go())

    assert bot.drawn.calls == []


def test_no_digest_channel_means_no_scheduled_digest(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Said out loud, because the alternative is a bot that silently never reports."""
    clock = FakeClock(at=datetime(2026, 7, 29, 7, 0, 1, tzinfo=CHICAGO))
    monkeypatch.setattr("pqn_whobot.whobot.datetime", clock)
    monkeypatch.setattr("pqn_whobot.whobot.SCHEDULER_TICK_S", 0.005)
    bot = FlowSpy(settings_for(schedule_hour=7))

    async def go() -> None:
        bot.start_scheduler()
        await settle()
        await bot.shutdown(grace_s=1.0)

    with caplog.at_level(logging.WARNING):
        asyncio.run(go())

    assert bot.drawn.calls == []
    assert "digest_channel" in caplog.text


# --------------------------------------------------------------------------------------
# The two schedule Actions.
# --------------------------------------------------------------------------------------


def fields_of(report: Report) -> dict[str, str]:
    return {f.name: f.value for f in report.sections[0].fields}


def test_digest_status_reports_a_run_that_happened(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(at=datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(
        clock, monkeypatch, schedule_hour=7, last_run_at=datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO), last_result="ok"
    )

    async def go() -> Report:
        bot.start_scheduler()
        report = await bot.digest_status()
        await bot.shutdown(grace_s=1.0)
        return report

    report = asyncio.run(go())

    assert report.status is Status.OK
    assert report.notes == []
    assert report.summary == "Scheduled for 07:00 America/Chicago."
    # Every time an operator is shown is in the digest's zone, never the host's.
    assert fields_of(report) == {
        "Next run": "2026-07-30 07:00 CDT",
        "Last run": "2026-07-29 07:00 CDT",
        "Last result": "ok",
    }


def test_digest_status_reports_a_missed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Derived from the two timestamps: nothing records that a run was skipped."""
    clock = FakeClock(at=datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7, last_run_at=datetime(2026, 7, 27, 12, 0, tzinfo=UTC))

    async def go() -> Report:
        bot.start_scheduler()
        report = await bot.digest_status()
        await bot.shutdown(grace_s=1.0)
        return report

    report = asyncio.run(go())

    assert report.status is Status.WARN
    assert report.notes == ["The 2026-07-29 07:00 CDT run did not happen; a missed run is never replayed."]


def test_digest_status_says_when_nothing_is_scheduled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bot with no digest_channel has no scheduler, and must not look healthy."""
    clock = FakeClock(at=datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))
    monkeypatch.setattr("pqn_whobot.whobot.datetime", clock)
    bot = FlowSpy(settings_for(schedule_hour=7))

    report = asyncio.run(bot.digest_status())

    assert report.status is Status.WARN
    assert fields_of(report)["Last run"] == "never"
    assert fields_of(report)["Last result"] == "nothing recorded"
    assert "no digest_channel" in report.notes[0]


def test_changing_the_schedule_persists_and_says_when_it_next_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(at=datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7)

    report = asyncio.run(bot.set_digest_schedule(hour=21, minute=15))

    assert report.status is Status.OK
    assert report.summary == "Scheduled for 21:15 America/Chicago, was 07:00 America/Chicago."
    assert fields_of(report) == {"Next run": "2026-07-29 21:15 CDT"}
    assert (bot.settings.schedule_hour, bot.settings.schedule_minute) == (21, 15)
    assert WhobotSettings().schedule_hour == 21  # noqa: PLR2004 - and it survives a restart


def test_the_schedule_form_opens_on_the_schedule_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(at=datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=21, schedule_minute=15)

    run(bot, PendingInvocation(action="set_digest_schedule"))

    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == {"hour": 21, "minute": 15}


@pytest.mark.parametrize(("hour", "minute"), [(24, 0), (-1, 0), (7, 60), (7, -1)])
def test_the_schedule_action_refuses_what_the_config_would(
    monkeypatch: pytest.MonkeyPatch, hour: int, minute: int
) -> None:
    """Two places state what a time of day is, so this keeps them from drifting apart.

    The Action must refuse anything ``WhobotSettings`` would, or the write succeeds and Whobot
    cannot load its own config on the next start.
    """
    clock = FakeClock(at=datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))
    bot = scheduled_bot(clock, monkeypatch, schedule_hour=7)

    report = asyncio.run(bot.set_digest_schedule(hour=hour, minute=minute))

    with pytest.raises(ValidationError):
        WhobotSettings(schedule_hour=hour, schedule_minute=minute)
    assert report.status is Status.FAIL
    assert (bot.settings.schedule_hour, bot.settings.schedule_minute) == (7, 0)
    assert not Path("whobot.toml").exists(), "a refused schedule must not touch the file"


# --------------------------------------------------------------------------------------
# Run SSM: the unattended Share a Secret Message game, by hand.
#
# Decision 13's report, decision 14's prefilled form and decision 15's leader-only scope,
# over a mocked Node API. The Nodes themselves are the 3.1 endpoint's own tests' business;
# here Whobot's half is pinned: what it asks for, and what it says about the answer.
# --------------------------------------------------------------------------------------

SSM_CALIBRATION = {
    "polarization_angles": {"h": 0.0, "v": 45.0, "d": 22.5, "a": -22.5},
    "reference_rates": {"vv": 3000.0, "vd": 1500.0, "dd": 750.0},
}


def ssm_api(
    *,
    mode: str = "entanglement",
    unattended: object = None,
    unattended_status: int = 200,
    calibration: object = SSM_CALIBRATION,
    seen: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the three endpoints a Run SSM touches, recording every call made.

    ``GET /node/operation_mode`` decides the prefill's pattern, ``POST /qkd/unattended`` is the
    game itself and ``GET /node/calibration`` supplies the Reference rates the correlation
    report shows beside each measured one.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path == "/node/operation_mode":
            return httpx.Response(200, json=mode)
        if path == "/node/calibration":
            return httpx.Response(200, json=calibration)
        if path == "/qkd/unattended":
            if unattended_status != HTTPStatus.OK:
                return httpx.Response(unattended_status, json={"detail": "the game did not finish"})
            return httpx.Response(HTTPStatus.OK, json=unattended)
        return httpx.Response(404)

    return handler


def entangled(leader_bits: list[int], follower_bits: list[int]) -> dict[str, object]:
    return {"mode": "entanglement", "leader_bits": leader_bits, "follower_bits": follower_bits}


def correlated(rates: list[float], levels: list[str]) -> dict[str, object]:
    return {"mode": "correlation", "rates": rates, "levels": levels}


def ssm_form(**overrides: object) -> dict[str, object]:
    """Return a complete Run SSM form, so a test changes only the field it is about."""
    return {
        "questions": 4,
        "leader_answers": "aabb",
        "follower_answers": "aabb",
        "max_error_rate": 0.15,
        "integration_time_s": 0.0,
        **overrides,
    }


def ssm_section(result: ActionResult) -> Section:
    """Return the single SSM section off the result, whichever Report shape it took."""
    assert isinstance(result, Report)
    return result.sections[0]


def run_ssm(bot: ClientSpy, *, node_url: str = ALICE, **overrides: object) -> Report:
    """Run Run SSM from a complete form and return the Report it posted."""
    asyncio.run(dispatched(bot, PendingInvocation(action="run_ssm", node_url=node_url, params=ssm_form(**overrides))))
    return only_report(bot)


# --- the prefill: config and the Node's Operation mode ---


def test_the_ssm_form_opens_on_the_entanglement_pattern() -> None:
    """Decision 13: all Questions matching, the first half both left and the rest both right."""
    seen: list[httpx.Request] = []
    bot = with_node_api(ssm_api(mode="entanglement", seen=seen), ALICE, ssm_questions=6, ssm_max_error_rate=0.2)

    asyncio.run(dispatched(bot, PendingInvocation(action="run_ssm", node_url=ALICE)))

    assert bot.drawn.calls == ["ask_for_params"]
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/node/operation_mode")]
    assert bot.drawn.initial == {
        "questions": 6,
        "leader_answers": "aaabbb",
        "follower_answers": "aaabbb",
        "max_error_rate": 0.2,
        "integration_time_s": 0.0,
    }


def test_the_ssm_form_opens_on_the_correlation_pattern() -> None:
    """Decision 13: the four Answer combinations in turn, so every expected level is exercised."""
    bot = with_node_api(ssm_api(mode="correlation"), ALICE, ssm_questions=8)

    asyncio.run(dispatched(bot, PendingInvocation(action="run_ssm", node_url=ALICE)))

    assert bot.drawn.initial["leader_answers"] == "aabbaabb"
    assert bot.drawn.initial["follower_answers"] == "abababab"
    assert bot.drawn.initial["questions"] == 8  # noqa: PLR2004 - the config's value


def test_an_odd_question_count_gives_the_extra_question_to_left() -> None:
    """A10: with an odd count the extra one is 'a', so 7 questions read "aaaabbb" on both sides.

    Handing the extra question to the right would read "aaabbbb", changing which basis gets the
    odd bit and so what the digest's two bases are compared over.
    """
    bot = with_node_api(ssm_api(mode="entanglement"), ALICE, ssm_questions=7)

    asyncio.run(dispatched(bot, PendingInvocation(action="run_ssm", node_url=ALICE)))

    assert bot.drawn.initial["leader_answers"] == "aaaabbb"
    assert bot.drawn.initial["follower_answers"] == "aaaabbb"


def test_the_ssm_form_falls_back_on_its_defaults_when_it_cannot_read_a_mode() -> None:
    """A form opening on defaults beats no form, so a Node that cannot be read must not block it."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(503, json={"detail": "down for maintenance"})

    bot = with_node_api(handler, ALICE, ssm_questions=8)

    asyncio.run(dispatched(bot, PendingInvocation(action="run_ssm", node_url=ALICE)))

    assert bot.drawn.calls == ["ask_for_params"]
    assert bot.drawn.initial == {
        "questions": 8,
        "leader_answers": "",
        "follower_answers": "",
        "max_error_rate": 0.15,
        "integration_time_s": 0.0,
    }


def test_the_ssm_form_asks_for_every_default_decision_14_names() -> None:
    """Questions, both players' Answers, the error threshold and the integration time, each a field."""
    act = WhobotSpy.actions["run_ssm"]

    assert act.label == "Run SSM"
    assert act.scope is Scope.ONE
    assert {parameter.name for parameter in act.parameters} == {
        "questions",
        "leader_answers",
        "follower_answers",
        "max_error_rate",
        "integration_time_s",
    }


# --- the payload ---


def test_running_ssm_sends_both_players_answers_and_the_integration_time() -> None:
    seen: list[httpx.Request] = []
    bot = with_node_api(
        ssm_api(unattended=entangled([0, 0], [0, 0]), seen=seen), ALICE, timetagger_address="10.0.0.5:9000"
    )

    run_ssm(bot, questions=2, leader_answers="ab", follower_answers="ba", integration_time_s=2.0)

    request = next(r for r in seen if r.url.path == "/qkd/unattended")
    assert request.method == "POST"
    assert json.loads(request.content) == {"leader_answers": ["a", "b"], "follower_answers": ["b", "a"]}
    assert request.url.params["timetagger_address"] == "10.0.0.5:9000"
    assert float(request.url.params["integration_time_s"]) == 2.0  # noqa: PLR2004 - the form's value


def test_running_ssm_sends_no_integration_time_when_the_field_is_zero() -> None:
    """Decision A9: 0 means the Node's own configured integration time, so no time is sent."""
    seen: list[httpx.Request] = []
    bot = with_node_api(ssm_api(unattended=entangled([0, 0], [0, 0]), seen=seen), ALICE)

    run_ssm(bot, questions=2, leader_answers="aa", follower_answers="aa", integration_time_s=0.0)

    request = next(r for r in seen if r.url.path == "/qkd/unattended")
    assert "integration_time_s" not in request.url.params


def test_upper_case_answers_are_sent_as_lower_case() -> None:
    """The Node accepts either case; Whobot normalises so the report's own comparisons agree."""
    seen: list[httpx.Request] = []
    bot = with_node_api(ssm_api(unattended=entangled([0, 0], [0, 0]), seen=seen), ALICE)

    run_ssm(bot, questions=2, leader_answers="AB", follower_answers="AB")

    request = next(r for r in seen if r.url.path == "/qkd/unattended")
    assert json.loads(request.content) == {"leader_answers": ["a", "b"], "follower_answers": ["a", "b"]}


def test_the_ssm_game_is_given_the_per_game_timeout_not_the_node_read_timeout() -> None:
    """A Game runs for minutes, so its call is bounded by ``per_game_timeout_s``, not the reads'.

    The mode and calibration reads spend ``node_timeout_s``; the game is the long call and must
    carry the longer bound, or a real game is cut off partway through.
    """
    seen: list[httpx.Request] = []
    bot = with_node_api(
        ssm_api(mode="correlation", unattended=correlated([3000.0], ["high"]), seen=seen),
        ALICE,
        node_timeout_s=7.5,
        per_game_timeout_s=901.0,
    )

    run_ssm(bot, questions=1, leader_answers="a", follower_answers="a")

    game = next(request for request in seen if request.url.path == "/qkd/unattended")
    reads = [request for request in seen if request.url.path != "/qkd/unattended"]
    assert game.extensions["timeout"] == httpx.Timeout(901.0).as_dict()
    assert reads, "the mode and calibration reads are separate calls"
    assert all(request.extensions["timeout"] == httpx.Timeout(7.5).as_dict() for request in reads)


# --- refusing Answers, without touching the Node ---


@pytest.mark.parametrize(
    ("form", "because"),
    [
        ({"questions": 2, "leader_answers": "aa", "follower_answers": "a"}, "same length"),
        ({"questions": 3, "leader_answers": "aa", "follower_answers": "aa"}, "do not match 3 questions"),
        ({"questions": 2, "leader_answers": "ac", "follower_answers": "ac"}, "must be a and b"),
        ({"questions": 2, "leader_answers": "aa", "follower_answers": "ax"}, "must be a and b"),
    ],
    ids=["unequal length", "wrong count", "bad leader Answer", "bad follower Answer"],
)
def test_the_ssm_action_refuses_answers_it_cannot_play(form: dict[str, object], because: str) -> None:
    """Decision 14: unequal-length Answers are refused, and so is anything else unplayable.

    A typo must be a clear refusal rather than a Node call that fails somewhere less useful — so
    the game is never started — and the refusal states the mode (decision 13), which means it
    does read the mode first.
    """
    seen: list[httpx.Request] = []
    bot = with_node_api(ssm_api(unattended=entangled([0, 0], [0, 0]), seen=seen), ALICE)

    report = run_ssm(bot, **form)

    assert report.status is Status.FAIL
    fields = {field.name: field for field in ssm_section(report).fields}
    assert because in fields["Answers"].value
    assert fields["Operation mode"].value == "entanglement"
    assert [request.url.path for request in seen] == ["/node/operation_mode"], "the game must not start"
    assert fields["Answers"].status is Status.FAIL, "a refused Answer is a failure, not a warning"


@pytest.mark.parametrize(
    ("leader", "follower"),
    [("aa", ""), ("", "aa")],
    ids=["empty follower", "empty leader"],
)
def test_one_empty_answer_field_is_refused_rather_than_filled_with_defaults(leader: str, follower: str) -> None:
    """A10: *both* fields empty means the mode's defaults; one empty is an unequal-length refusal.

    Reading one empty field as "both empty" would silently play the mode's own Answers for a
    game the operator only half asked for, hiding their mistake instead of reporting it.
    """
    seen: list[httpx.Request] = []
    bot = with_node_api(ssm_api(unattended=entangled([0, 0], [0, 0]), seen=seen), ALICE)

    report = run_ssm(bot, questions=2, leader_answers=leader, follower_answers=follower)

    assert report.status is Status.FAIL
    assert "same length" in {field.name: field for field in ssm_section(report).fields}["Answers"].value
    assert "/qkd/unattended" not in [request.url.path for request in seen], "no game was played"


# --- the report, entanglement mode ---


def test_an_entanglement_ssm_report_names_the_node_it_checked() -> None:
    """An operator runs Run SSM on one Node, so the title must say which leader was checked."""
    bot = with_node_api(ssm_api(unattended=entangled([0, 1, 0, 1], [0, 1, 0, 1])), ALICE)

    report = run_ssm(bot)

    assert report.title == f"{GAME_TITLES['ssm']} — uiuc-public-left"


def test_an_entanglement_ssm_report_shows_the_error_rate_per_basis() -> None:
    """Decision 13: the mode is stated, and each basis gets its own bit error rate."""
    bot = with_node_api(ssm_api(unattended=entangled([0, 1, 0, 1], [0, 1, 0, 1])), ALICE)

    report = run_ssm(bot)

    assert report.status is Status.OK
    section = ssm_section(report)
    assert section.label == GAME_TITLES["ssm"]
    assert section.status is Status.OK
    fields = {field.name: field for field in section.fields}
    assert fields["Operation mode"].value == "entanglement"
    assert fields["H/V error rate"].value == "0.0000 (0 of 2 bits mismatched)"
    assert fields["H/V error rate"].status is Status.OK
    assert fields["D/A error rate"].status is Status.OK
    assert fields["Max error rate"].value == "0.15"


def test_an_entanglement_ssm_report_warns_when_a_basis_error_rate_is_above_the_threshold() -> None:
    """One of the two H/V bits is wrong: 0.5 is above 0.15, so the check WARNs."""
    bot = with_node_api(ssm_api(unattended=entangled([0, 1, 0, 1], [1, 1, 0, 1])), ALICE)

    report = run_ssm(bot)

    section = ssm_section(report)
    assert section.status is Status.WARN
    fields = {field.name: field for field in section.fields}
    assert fields["H/V error rate"].value == "0.5000 (1 of 2 bits mismatched)"
    assert fields["H/V error rate"].status is Status.WARN
    assert fields["D/A error rate"].status is Status.OK


def test_a_basis_under_the_threshold_is_ok_and_the_report_says_the_threshold() -> None:
    """Inclusive: a rate exactly at the threshold is OK, since decision 13 says at or under it."""
    bot = with_node_api(ssm_api(unattended=entangled([0, 1, 0, 1], [1, 1, 0, 1])), ALICE)

    report = run_ssm(bot, max_error_rate=0.5)

    fields = {field.name: field for field in ssm_section(report).fields}
    assert fields["H/V error rate"].status is Status.OK
    assert fields["Max error rate"].value == "0.5"


def test_an_entanglement_ssm_report_warns_for_a_basis_with_no_compared_question() -> None:
    """Agent decision A11: a basis no question compared is not under the threshold, so it WARNs.

    With Answers that never match on a basis, nothing there could be compared; an unchecked
    basis must not read as OK.
    """
    bot = with_node_api(ssm_api(unattended=entangled([0, 0], [0, 0])), ALICE)

    report = run_ssm(bot, questions=2, leader_answers="ab", follower_answers="ba")

    section = ssm_section(report)
    assert section.status is Status.WARN
    fields = {field.name: field for field in section.fields}
    assert fields["H/V error rate"].value == "not compared"
    assert fields["H/V error rate"].status is Status.WARN
    assert fields["D/A error rate"].value == "not compared"


# --- the report, correlation mode ---


def test_a_correlation_ssm_report_shows_each_rate_beside_its_reference() -> None:
    """Decision 13: expected level from the Answers, measured level next to it, rate by reference."""
    seen: list[httpx.Request] = []
    bot = with_node_api(
        ssm_api(
            mode="correlation",
            unattended=correlated([3000.0, 1500.0, 1500.0, 750.0], ["high", "medium", "medium", "low"]),
            seen=seen,
        ),
        ALICE,
    )

    report = run_ssm(bot, leader_answers="aabb", follower_answers="abab")

    section = ssm_section(report)
    assert section.status is Status.OK
    fields = {field.name: field for field in section.fields}
    assert fields["Operation mode"].value == "correlation"
    assert fields["Q1"].value == "3000 cps, reference 3000, measured high, expected high"
    assert fields["Q2"].value == "1500 cps, reference 1500, measured medium, expected medium"
    assert fields["Q4"].value == "750 cps, reference 750, measured low, expected low"
    assert all(field.status is Status.OK for name, field in fields.items() if name.startswith("Q"))
    assert [request.url.path for request in seen if request.url.path == "/node/calibration"] == ["/node/calibration"]


def test_a_correlation_ssm_report_warns_when_a_question_is_misclassified() -> None:
    """A question whose measured level is not the expected one is a WARN, and the rate shows why."""
    bot = with_node_api(
        ssm_api(
            mode="correlation",
            unattended=correlated([3000.0, 3000.0, 1500.0, 750.0], ["high", "high", "medium", "low"]),
        ),
        ALICE,
    )

    report = run_ssm(bot, leader_answers="aabb", follower_answers="abab")

    section = ssm_section(report)
    assert section.status is Status.WARN
    fields = {field.name: field for field in section.fields}
    assert fields["Q2"].value == "3000 cps, reference 1500, measured high, expected medium"
    assert fields["Q2"].status is Status.WARN
    assert fields["Q3"].status is Status.OK


def test_a_correlation_report_says_so_when_the_reference_rates_cannot_be_read() -> None:
    """A Reference rate that cannot be read is shown as unknown, and does not change the verdict.

    Decision 13: OK when every question gets its *expected* Coincidence level. With the level
    right, the missing Reference rate is a gap in the row's text, not a WARN.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/node/calibration":
            return httpx.Response(503, json={"detail": "not set"})
        return ssm_api(mode="correlation", unattended=correlated([3000.0], ["high"]))(request)

    bot = with_node_api(handler, ALICE)

    report = run_ssm(bot, questions=1, leader_answers="a", follower_answers="a")

    section = ssm_section(report)
    fields = {field.name: field for field in section.fields}
    assert fields["Q1"].value == "3000 cps, reference unknown, measured high, expected high"
    assert fields["Q1"].status is Status.OK
    assert section.status is Status.OK


def test_a_misclassified_question_is_a_warn_with_or_without_a_reference_rate() -> None:
    """The verdict follows the level comparison alone, so an unread Reference rate cannot hide it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/node/calibration":
            return httpx.Response(503, json={"detail": "not set"})
        return ssm_api(mode="correlation", unattended=correlated([3000.0], ["medium"]))(request)

    bot = with_node_api(handler, ALICE)

    report = run_ssm(bot, questions=1, leader_answers="a", follower_answers="a")

    fields = {field.name: field for field in ssm_section(report).fields}
    assert fields["Q1"].value == "3000 cps, reference unknown, measured medium, expected high"
    assert fields["Q1"].status is Status.WARN
    assert ssm_section(report).status is Status.WARN


# --- the game not completing ---


def test_an_ssm_game_that_does_not_complete_is_a_fail() -> None:
    """Decision 13: FAIL when the game does not complete, with the Node's own reason and the mode."""
    bot = with_node_api(ssm_api(unattended_status=500), ALICE)

    report = run_ssm(bot)

    section = ssm_section(report)
    assert report.status is Status.FAIL
    assert section.status is Status.FAIL
    fields = {field.name: field for field in section.fields}
    assert fields["Operation mode"].value == "entanglement"
    assert fields["Result"].value == "The Game did not complete."
    assert section.error is not None
    assert "the game did not finish" in section.error


def test_a_node_that_answers_an_unattended_game_with_something_else_is_a_fail() -> None:
    """Something else on that port must not be read as an SSM result, and the mode is stated."""
    bot = with_node_api(ssm_api(unattended={"hello": "world"}), ALICE)

    report = run_ssm(bot)

    section = ssm_section(report)
    assert section.status is Status.FAIL
    assert {field.name for field in section.fields} == {"Operation mode", "Result"}


def test_a_failed_ssm_game_whose_mode_reports_correlation_states_correlation() -> None:
    """The mode on a FAIL comes from the Node, so a correlation-mode failure says so."""
    bot = with_node_api(ssm_api(mode="correlation", unattended_status=500), ALICE)

    report = run_ssm(bot, leader_answers="aabb", follower_answers="abab")

    fields = {field.name: field for field in ssm_section(report).fields}
    assert fields["Operation mode"].value == "correlation"


def test_the_ssm_action_reports_only_the_leader_it_was_given() -> None:
    """Decision 15: Run SSM picks one Node; the Node resolves its own configured follower."""
    act = WhobotSpy.actions["run_ssm"]
    assert act.scope is Scope.ONE


# --- empty Answers: the mode's own default pattern, per mode (A10) ---


def test_empty_answers_play_the_entanglement_pattern() -> None:
    """A10: both fields empty, so the game plays all-matching Answers for four questions."""
    seen: list[httpx.Request] = []
    bot = with_node_api(
        ssm_api(mode="entanglement", unattended=entangled([0, 0, 1, 1], [0, 0, 1, 1]), seen=seen), ALICE
    )

    report = run_ssm(bot, questions=4, leader_answers="", follower_answers="")

    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/node/operation_mode"),
        ("POST", "/qkd/unattended"),
    ]
    game = next(request for request in seen if request.url.path == "/qkd/unattended")
    assert json.loads(game.content) == {
        "leader_answers": ["a", "a", "b", "b"],
        "follower_answers": ["a", "a", "b", "b"],
    }
    assert report.status is Status.OK


def test_empty_answers_play_the_correlation_pattern() -> None:
    """A10: the four Answer combinations in turn, so every expected level is exercised."""
    seen: list[httpx.Request] = []
    bot = with_node_api(
        ssm_api(
            mode="correlation",
            unattended=correlated([3000.0, 1500.0, 1500.0, 750.0], ["high", "medium", "medium", "low"]),
            seen=seen,
        ),
        ALICE,
    )

    report = run_ssm(bot, questions=4, leader_answers="", follower_answers="")

    game = next(request for request in seen if request.url.path == "/qkd/unattended")
    assert json.loads(game.content) == {
        "leader_answers": ["a", "a", "b", "b"],
        "follower_answers": ["a", "b", "a", "b"],
    }
    assert report.status is Status.OK


def test_empty_answers_with_an_unreadable_mode_are_a_fail_with_no_game() -> None:
    """With no Answers to play, the mode is the only source of them; a failed read is a FAIL."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/node/operation_mode":
            return httpx.Response(503, json={"detail": "down for maintenance"})
        return httpx.Response(404)

    bot = with_node_api(handler, ALICE)

    report = run_ssm(bot, questions=4, leader_answers="", follower_answers="")

    section = ssm_section(report)
    assert report.status is Status.FAIL
    fields = {field.name: field for field in section.fields}
    assert fields["Operation mode"].value == "unknown"
    assert [request.url.path for request in seen] == ["/node/operation_mode"], "no game is started"


def test_typed_answers_with_an_unreadable_mode_still_play() -> None:
    """The mode only fills in defaults, so Answers an operator typed let the game run anyway."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/node/operation_mode":
            return httpx.Response(503, json={"detail": "down for maintenance"})
        if request.url.path == "/qkd/unattended":
            return httpx.Response(200, json=entangled([0, 1, 0, 1], [0, 1, 0, 1]))
        return httpx.Response(404)

    bot = with_node_api(handler, ALICE)

    report = run_ssm(bot, questions=4, leader_answers="aabb", follower_answers="aabb")

    assert report.status is Status.OK
    assert "/qkd/unattended" in [request.url.path for request in seen]
    # The game's own answer is the authority on the mode, so the report still states it.
    assert {field.name: field for field in ssm_section(report).fields}["Operation mode"].value == "entanglement"


def test_a_failed_game_with_an_unreadable_mode_says_the_mode_is_unknown() -> None:
    """When the Node cannot be read and the game fails, the mode row says "unknown", not a guess.

    Saying "entanglement" would assert a fact nobody established; the report must admit it does
    not know the mode rather than claim the default.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/node/operation_mode":
            return httpx.Response(503, json={"detail": "down for maintenance"})
        if request.url.path == "/qkd/unattended":
            return httpx.Response(500, json={"detail": "the game did not finish"})
        return httpx.Response(404)

    bot = with_node_api(handler, ALICE)

    report = run_ssm(bot, questions=2, leader_answers="aa", follower_answers="aa")

    section = ssm_section(report)
    assert report.status is Status.FAIL
    fields = {field.name: field for field in section.fields}
    assert fields["Operation mode"].value == "unknown"
    assert "/qkd/unattended" in [request.url.path for request in seen]


# --- the partly compared basis: unmatched questions never count (test-reviewer-glm F3) ---


def test_a_partly_compared_basis_counts_only_the_matched_questions() -> None:
    """A basis with one compared question reads that one question, not the whole basis.

    Answers "abab" against "aabb" match on Q1 (H/V) and Q4 (D/A) only; Q2 and Q3 are played
    but never compared. Their bits are made to differ, so counting them would show a nonzero
    rate on both bases; the report counts each basis' own compared question alone.
    """
    bot = with_node_api(ssm_api(unattended=entangled([0, 0, 0, 0], [0, 1, 1, 0])), ALICE)

    report = run_ssm(bot, questions=4, leader_answers="abab", follower_answers="aabb")

    section = ssm_section(report)
    assert section.status is Status.OK
    fields = {field.name: field for field in section.fields}
    assert fields["H/V error rate"].value == "0.0000 (0 of 1 bits mismatched)"
    assert fields["D/A error rate"].value == "0.0000 (0 of 1 bits mismatched)"


def test_a_partly_compared_basis_counts_a_mismatch_on_its_compared_question() -> None:
    """The mirrored case: the one compared question of each basis is mismatched, so both warn."""
    bot = with_node_api(ssm_api(unattended=entangled([1, 0, 0, 1], [0, 1, 1, 0])), ALICE)

    report = run_ssm(bot, questions=4, leader_answers="abab", follower_answers="aabb")

    section = ssm_section(report)
    fields = {field.name: field for field in section.fields}
    assert fields["H/V error rate"].value == "1.0000 (1 of 1 bits mismatched)"
    assert fields["D/A error rate"].value == "1.0000 (1 of 1 bits mismatched)"
    assert section.status is Status.WARN


# --- the time bound follows what the Action spends (reviewer-qwen F1) ---


def test_the_ssm_action_is_allowed_its_game_and_both_its_node_reads() -> None:
    """The bound is the sum of what Run SSM spends: the game and two Node reads (TimeoutSpec).

    Unlike Run CHSH's ``one_game_budget``, Run SSM also reads the Operation mode and the
    Reference rates, so its bound must carry those two calls or a completed game is thrown away.
    """
    settings = settings_for(node_timeout_s=30.0, per_game_timeout_s=600.0)

    assert ssm_budget(settings) == pytest.approx(2 * 30.0 + 600.0)
    assert FlowSpy.actions["run_ssm"].timeout_for(settings) == pytest.approx(ssm_budget(settings))
    assert ssm_budget(settings) > one_game_budget(settings), "it spends more than Run CHSH"


def test_the_ssm_bound_follows_the_configuration_it_spends() -> None:
    """Raise either timeout and Run SSM's bound grows with it, with nothing else to edit."""
    act = FlowSpy.actions["run_ssm"]

    assert act.timeout_for(settings_for(node_timeout_s=10.0, per_game_timeout_s=900.0)) == pytest.approx(920.0)
    assert ssm_budget(settings_for(node_timeout_s=7.5, per_game_timeout_s=100.0)) == pytest.approx(115.0)


# --------------------------------------------------------------------------------------
# angle_sweep: one call to the leader, a plot back.
# --------------------------------------------------------------------------------------


def _sweep_request(form: dict[str, object]) -> httpx.Request:
    """Run an Angle Sweep from this form and return the call it made to the leader."""
    seen: list[httpx.Request] = []
    bot = with_node_api(node_api(seen=seen), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="angle_sweep", node_url=ALICE, params=form)))
    return next(r for r in seen if r.url.path == "/chsh/sweep")


def test_angle_sweep_sends_the_form_values_to_the_leader() -> None:
    request = _sweep_request(SWEEP_FORM)

    assert request.method == "POST"
    assert float(request.url.params["step_deg"]) == SWEEP_FORM["step_deg"]
    assert float(request.url.params["integration_time_s"]) == SWEEP_FORM["integration_time_s"]


@pytest.mark.parametrize(("polarization", "angle"), [("H", 0.0), ("D", 45.0), ("V", 90.0), ("A", 315.0)])
def test_a_named_polarization_pins_the_follower_at_its_angle(polarization: str, angle: float) -> None:
    request = _sweep_request({**SWEEP_FORM, "follower_polarization": polarization, "custom_angle": 37.5})

    assert float(request.url.params["follower_angle"]) == angle


def test_a_custom_polarization_pins_the_follower_at_the_custom_angle() -> None:
    custom_angle = 37.5
    request = _sweep_request({**SWEEP_FORM, "follower_polarization": "custom", "custom_angle": custom_angle})

    assert float(request.url.params["follower_angle"]) == custom_angle


def test_angle_sweep_plots_the_counts_and_reports_visibility() -> None:
    bot = with_node_api(node_api(), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="angle_sweep", node_url=ALICE, params=SWEEP_FORM)))
    result = only_report(bot)

    assert result.status is Status.OK
    assert "alice (leader)" in result.title
    assert "bob (follower)" in result.title
    assert result.image is not None
    assert result.image.startswith(b"\x89PNG\r\n\x1a\n")
    fields = {f.name: f.value for f in result.sections[0].fields}
    assert fields["Visibility"] == f"{90 / 110:.4f}"


def test_angle_sweep_appends_its_data_beside_the_config() -> None:
    """The flow tests run in a temporary directory, so this is the file a real run would write."""
    bot = with_node_api(node_api(), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="angle_sweep", node_url=ALICE, params=SWEEP_FORM)))

    with Path("whobot_sweeps.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [int(row["coincidences"]) for row in rows] == SWEEP["coincidences"]
    assert any("whobot_sweeps.csv" in note for note in only_report(bot).notes)


def test_a_sweep_the_leader_refuses_is_a_fail_with_its_reason() -> None:
    bot = with_node_api(node_api(refuse={"/chsh/sweep": 503}), ALICE)
    asyncio.run(dispatched(bot, PendingInvocation(action="angle_sweep", node_url=ALICE, params=SWEEP_FORM)))
    result = only_report(bot)

    assert result.status is Status.FAIL
    assert result.image is None
    assert "not having it" in (result.sections[0].error or "")
