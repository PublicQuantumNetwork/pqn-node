"""Tests for the Slack layer: the renderers, the widgets, and the guards around them.

Renderers are pure ``ActionResult -> blocks``, which is why they can be called directly
here with no socket, no token and no Node. What is worth pinning is the handful of Slack
limits that produce useless errors when breached — a 75-character option value, ten fields
to a section — and the two rendering rules that come from the data rather than from a flag.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from pqn_whobot.actions import ActionResult
from pqn_whobot.actions import Field
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import Report
from pqn_whobot.actions import Section
from pqn_whobot.actions import Status
from pqn_whobot.actions import scan_actions
from pqn_whobot.config import NodeEntry
from pqn_whobot.config import WhobotSettings
from pqn_whobot.registry import Node
from pqn_whobot.whobot_slack import FIELDS_PER_SECTION
from pqn_whobot.whobot_slack import HEADER_LIMIT
from pqn_whobot.whobot_slack import OPTION_VALUE_LIMIT
from pqn_whobot.whobot_slack import PARAMS_BLOCK
from pqn_whobot.whobot_slack import STATUS_EMOJI
from pqn_whobot.whobot_slack import SlackReply
from pqn_whobot.whobot_slack import WhobotSlack
from pqn_whobot.whobot_slack import _image_suffix
from pqn_whobot.whobot_slack import _option_value

ALICE = "http://node-a.invalid:9000"

Block = dict[str, Any]


@pytest.fixture(autouse=True)
def _in_a_temp_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a real ./whobot.toml out of these tests, as it outranks keyword arguments."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def bot() -> WhobotSlack:
    """Build a real WhobotSlack. It creates an AsyncApp but never connects, so the token is a placeholder."""
    return WhobotSlack(WhobotSettings(slack_bot_token="xoxb-not-a-real-token", nodes=[NodeEntry(api_url=ALICE)]))  # noqa: S106


def texts(blocks: list[Block]) -> str:
    """Everything renderable in one string, for asserting that something appears at all."""
    return json.dumps(blocks)


# --------------------------------------------------------------------------------------
# Report rendering.
# --------------------------------------------------------------------------------------


def test_a_report_renders_a_header_and_a_summary(bot: WhobotSlack) -> None:
    blocks = bot._render(Report(status=Status.OK, title="Nodes", summary="2 of 2 reachable"))  # noqa: SLF001
    assert blocks[0]["type"] == "header"
    assert "Nodes" in blocks[0]["text"]["text"]
    assert blocks[1]["text"]["text"] == "2 of 2 reachable"


def test_a_field_with_a_status_renders_as_a_checklist_line(bot: WhobotSlack) -> None:
    """The rule the whole two-mode layout rests on, derived from the data not a flag."""
    report = Report(
        status=Status.OK,
        title="Nodes",
        sections=[Section(fields=[Field(name="Router", value="12ms", status=Status.OK)])],
    )
    lines = [b for b in bot._render(report) if "fields" not in b and b["type"] == "section"]  # noqa: SLF001
    assert lines[-1]["text"]["text"] == ":white_check_mark: Router — 12ms"


def test_a_field_without_a_status_renders_in_a_grid(bot: WhobotSlack) -> None:
    report = Report(
        status=Status.OK,
        title="Info",
        sections=[Section(fields=[Field(name="Name", value="uiuc-public-left")])],
    )
    grids = [b for b in bot._render(report) if "fields" in b]  # noqa: SLF001
    assert len(grids) == 1
    assert grids[0]["fields"][0]["text"] == "*Name*\nuiuc-public-left"


def test_fourteen_measurements_split_into_blocks_of_ten_and_four(bot: WhobotSlack) -> None:
    """Slack caps a section at ten fields. An Action emits one Section; this splits it."""
    fields = [Field(name=f"m{i}", value=str(i)) for i in range(14)]
    report = Report(status=Status.OK, title="Many", sections=[Section(fields=fields)])
    grids = [b for b in bot._render(report) if "fields" in b]  # noqa: SLF001
    assert [len(b["fields"]) for b in grids] == [FIELDS_PER_SECTION, 4]


def test_the_chunking_limit_is_slacks_and_not_the_actions(bot: WhobotSlack) -> None:
    """An Action emitting exactly ten stays in one block; the eleventh starts a second."""
    ten = [Field(name=f"m{i}", value=str(i)) for i in range(FIELDS_PER_SECTION)]
    report = Report(status=Status.OK, title="Ten", sections=[Section(fields=ten)])
    assert len([b for b in bot._render(report) if "fields" in b]) == 1  # noqa: SLF001


@pytest.mark.parametrize(
    ("status", "emoji"),
    [
        (Status.OK, ":white_check_mark:"),
        (Status.WARN, ":warning:"),
        (Status.FAIL, ":x:"),
        (Status.SKIPPED, ":grey_question:"),
        (Status.ON, ":large_green_circle:"),
        (Status.OFF, ":red_circle:"),
    ],
)
def test_each_status_renders_as_its_own_emoji(bot: WhobotSlack, status: Status, emoji: str) -> None:
    """SKIPPED must not look like FAIL: did-not-run and ran-and-failed are different news."""
    report = Report(status=Status.OK, title="T", sections=[Section(fields=[Field(name="x", value="y", status=status)])])
    assert emoji in texts(bot._render(report))  # noqa: SLF001


def test_every_status_has_a_glyph() -> None:
    """``_emoji`` falls back to no glyph, so a missing one drops the marker silently."""
    assert set(STATUS_EMOJI) == set(Status)
    assert len(set(STATUS_EMOJI.values())) == len(Status), "two statuses share a glyph"


def test_skipped_renders_differently_from_fail(bot: WhobotSlack) -> None:
    def one(status: Status) -> str:
        report = Report(
            status=Status.OK, title="T", sections=[Section(fields=[Field(name="x", value="y", status=status)])]
        )
        return texts(bot._render(report))  # noqa: SLF001

    assert one(Status.SKIPPED) != one(Status.FAIL)


def test_a_section_note_and_error_render_as_different_blocks(bot: WhobotSlack) -> None:
    """A footer and a traceback look nothing alike, which is why they are separate fields."""
    report = Report(
        status=Status.FAIL,
        title="CHSH",
        sections=[Section(note="34.2s", error="Traceback (most recent call last): ...")],
    )
    blocks = bot._render(report)  # noqa: SLF001
    assert any(b["type"] == "context" and "34.2s" in texts([b]) for b in blocks)
    assert any(b["type"] == "section" and "```" in texts([b]) for b in blocks)


def test_report_notes_render_as_a_footer(bot: WhobotSlack) -> None:
    report = Report(status=Status.OK, title="T", notes=["Applied without a restart."])
    assert "Applied without a restart." in texts(bot._render(report))  # noqa: SLF001


def test_a_long_title_is_truncated_rather_than_rejected(bot: WhobotSlack) -> None:
    """Slack rejects an over-long header outright, and losing the whole result is worse."""
    blocks = bot._render(Report(status=Status.OK, title="T" * 400))  # noqa: SLF001
    assert len(blocks[0]["text"]["text"]) <= HEADER_LIMIT


def test_slack_control_characters_in_a_value_are_escaped(bot: WhobotSlack) -> None:
    """A Node error containing < or & must not be read as markup."""
    report = Report(
        status=Status.FAIL,
        title="T",
        sections=[Section(fields=[Field(name="Error", value="a < b & c")])],
    )
    assert "a &lt; b &amp; c" in texts(bot._render(report))  # noqa: SLF001


# --------------------------------------------------------------------------------------
# Every result type must be renderable.
# --------------------------------------------------------------------------------------


def concrete_result_types() -> list[type[ActionResult]]:
    """Every shipped ActionResult subclass, found rather than listed.

    Restricted to ``pqn_whobot``: a throwaway subclass declared inside another test stays
    in ``__subclasses__`` until it is collected, which would otherwise make this pass or
    fail depending on the order pytest-randomly picked.
    """

    def walk(cls: type[ActionResult]) -> list[type[ActionResult]]:
        found = []
        for sub in cls.__subclasses__():
            found += walk(sub)
            if sub.__module__.startswith("pqn_whobot."):
                found.append(sub)
        return found

    return walk(ActionResult)


def test_every_action_result_subclass_has_a_renderer() -> None:
    """Scanned, not maintained as a list, so a new result type fails pytest not an operator."""
    # Reached through __dict__ because attribute access on the class returns the bound
    # descriptor's result rather than the singledispatchmethod holding the registry.
    registry = WhobotSlack.__dict__["_render"].dispatcher.registry
    unrenderable = [cls.__name__ for cls in concrete_result_types() if cls not in registry]
    assert not unrenderable, f"no renderer registered for: {unrenderable}"


def test_a_result_type_with_no_renderer_fails_loudly(bot: WhobotSlack) -> None:
    """The fallback must refuse to guess, so the test above is what catches a gap."""

    class Unrenderable(ActionResult):
        pass

    with pytest.raises(NotImplementedError, match="no renderer registered"):
        bot._render(Unrenderable())  # noqa: SLF001


# --------------------------------------------------------------------------------------
# Uploading an image.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("image", "suffix"),
    [
        (b"\x89PNG\r\n\x1a\nrest of a screenshot", "png"),
        (b"GIF89a and the rest", "gif"),
        (b"\xff\xd8\xff\xe0 and the rest", "jpg"),
    ],
)
def test_an_upload_is_named_after_what_its_bytes_are(image: bytes, suffix: str) -> None:
    """Slack picks how to display a file from its filename, so a GIF sent as .png arrives broken."""
    assert _image_suffix(image) == suffix


def test_an_unrecognised_image_is_uploaded_as_a_png() -> None:
    """Every Action that produces one produces a PNG; the fallback must not be an error."""
    assert _image_suffix(b"something else entirely") == "png"


# --------------------------------------------------------------------------------------
# The payload guards.
# --------------------------------------------------------------------------------------


def test_an_option_value_within_slacks_limit_is_returned_unchanged() -> None:
    pending = PendingInvocation(action="set_availability", node_url=ALICE)
    assert _option_value(pending, "menu") == '{"a":"set_availability","n":"http://node-a.invalid:9000"}'


def test_an_over_long_option_value_raises_naming_what_was_being_rendered() -> None:
    """Slack answers an over-long value with a bare invalid_blocks, which names nothing."""
    pending = PendingInvocation(action="x" * 60, node_url="http://" + "y" * 60)
    with pytest.raises(ValueError, match=r"menu entry 'x+'.*option value limit"):
        _option_value(pending, "menu entry " + repr("x" * 60))


def test_the_limit_guard_measures_the_encoding_and_not_the_action_name() -> None:
    borderline = PendingInvocation(action="a" * OPTION_VALUE_LIMIT)
    with pytest.raises(ValueError, match="over Slack's"):
        _option_value(borderline, "menu")


# --------------------------------------------------------------------------------------
# Widgets built from an Action's declaration.
# --------------------------------------------------------------------------------------


def availability_action() -> Any:
    return scan_actions(WhobotSlack)["set_availability"]


def test_a_bool_parameter_becomes_a_checkbox(bot: WhobotSlack) -> None:
    block = bot._checkbox_block(availability_action(), {})  # noqa: SLF001
    assert block["element"]["type"] == "checkboxes"
    assert [o["value"] for o in block["element"]["options"]] == ["chsh", "qf", "ssm"]


def test_the_form_opens_ticked_on_the_values_it_was_given(bot: WhobotSlack) -> None:
    """The prefill's whole purpose: the form shows the Node's flags, not the defaults."""
    block = bot._checkbox_block(availability_action(), {"chsh": False, "qf": True, "ssm": False})  # noqa: SLF001
    assert [o["value"] for o in block["element"]["initial_options"]] == ["qf"]


def test_a_form_with_nothing_ticked_omits_initial_options(bot: WhobotSlack) -> None:
    """Slack rejects an empty initial_options outright rather than treating it as none."""
    block = bot._checkbox_block(availability_action(), {"chsh": False, "qf": False, "ssm": False})  # noqa: SLF001
    assert "initial_options" not in block["element"]


def submitted(*ticked: str) -> dict[str, Any]:
    """Build a view submission's state as Slack really sends it.

    Only ``selected_options`` comes back. ``initial_options`` is part of the block that was
    *sent* and is not echoed, which an earlier version of these tests assumed it was — so the
    False floor looked reconstructable from the payload when it is not.
    """
    return {PARAMS_BLOCK: {PARAMS_BLOCK: {"type": "checkboxes", "selected_options": [{"value": t} for t in ticked]}}}


def test_an_unticked_checkbox_comes_back_as_false_rather_than_missing(bot: WhobotSlack) -> None:
    """Slack reports only what is ticked, so absence has to be reconstructed as False."""
    read = bot._read_checkboxes(availability_action(), submitted("qf")[PARAMS_BLOCK])  # noqa: SLF001
    assert read == {"chsh": False, "qf": True, "ssm": False}


def test_a_form_submitted_with_everything_unticked_reads_as_all_false(bot: WhobotSlack) -> None:
    """The regression that mattered: an unticked box must not fall through to its default.

    Every default on ``set_availability`` is True, so a payload of ``{}`` plus
    ``coerce_params`` used to turn "switch everything off" into "switch everything on".
    """
    read = bot._read_checkboxes(availability_action(), submitted()[PARAMS_BLOCK])  # noqa: SLF001
    assert read == {"chsh": False, "qf": False, "ssm": False}


def test_a_cleared_box_survives_coerce_params_as_false(bot: WhobotSlack) -> None:
    """The floor is only worth anything if it reaches the Action's arguments."""
    act = availability_action()
    arguments = act.coerce_params(bot._read_checkboxes(act, submitted("qf")[PARAMS_BLOCK]))  # noqa: SLF001
    assert arguments == {"chsh": False, "qf": True, "ssm": False}


# --------------------------------------------------------------------------------------
# The reply handle.
# --------------------------------------------------------------------------------------


def test_the_base_class_handle_is_rejected_rather_than_silently_mishandled(bot: WhobotSlack) -> None:
    from pqn_whobot.actions import ReplyHandle  # noqa: PLC0415

    with pytest.raises(TypeError, match="not a SlackReply"):
        bot._slack(ReplyHandle())  # noqa: SLF001


def test_a_slack_reply_carries_where_a_result_belongs() -> None:
    reply = SlackReply(channel="C123", thread_ts="1700000000.000100")
    assert reply.channel == "C123"
    assert reply.thread_ts == "1700000000.000100"


def test_targets_are_labelled_with_both_name_and_address() -> None:
    """A name can be absent or duplicated, so the address is always shown beside it."""
    from pqn_whobot.whobot_slack import _target_label  # noqa: PLC0415

    assert _target_label(Node(api_url=ALICE, name="uiuc-public-left", reachable=True)) == f"uiuc-public-left — {ALICE}"
    assert _target_label(Node(api_url=ALICE, reachable=False)) == f"(unknown) — {ALICE}"
