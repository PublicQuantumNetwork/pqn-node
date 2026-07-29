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
import re
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import httpx
import pytest

from pqn_node.core.config import GamesAvailability
from pqn_whobot.actions import Action
from pqn_whobot.actions import ActionResult
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import ReplyHandle
from pqn_whobot.actions import Report
from pqn_whobot.actions import Scope
from pqn_whobot.actions import Status
from pqn_whobot.actions import action
from pqn_whobot.actions import prefill
from pqn_whobot.config import NodeEntry
from pqn_whobot.config import WhobotSettings
from pqn_whobot.node_client import NodeClient
from pqn_whobot.registry import Node
from pqn_whobot.whobot import Whobot

ALICE = "http://node-a.invalid:9000"
BOB = "http://node-b.invalid:9000"

REACHABLE = [
    Node(api_url=ALICE, name="uiuc-public-left", reachable=True, latency_ms=18.0),
    Node(api_url=BOB, name="ufl-public-right", reachable=True, latency_ms=24.0),
]

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

    def _client(self, node: Node, timeout_s: float | None = None) -> NodeClient:
        return NodeClient(node.api_url, timeout_s or 5.0, transport=httpx.MockTransport(self.handler))


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


def _human_strings(report: Report) -> list[str]:
    """Every string an operator reads, except ``Section.error``, which may hold a traceback."""
    strings = [report.title, *([report.summary] if report.summary else []), *report.notes]
    for section in report.sections:
        strings += [text for text in (section.label, section.note) if text]
        strings += [f.name for f in section.fields]
        strings += [f.value for f in section.fields]
    return strings


def assert_no_markup(report: Report) -> None:
    for text in _human_strings(report):
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
