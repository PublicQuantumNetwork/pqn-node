"""Tests for the Action contract: the result vocabulary, the scan, and the payload codec.

The scan's job is to reject a malformed Action at import, so most of what is pinned here is
a *failure*: the message an author gets, and that they get it at all. Every case runs
``scan_actions`` directly on a throwaway class, so none of it needs a Whobot, a Node, or
Slack. That the scan also runs on class creation is pinned in the ``whobot.py`` tests.
"""

import asyncio
import json

import pytest

from pqn_whobot.actions import ActionDeclarationError
from pqn_whobot.actions import Field
from pqn_whobot.actions import PayloadError
from pqn_whobot.actions import PendingInvocation
from pqn_whobot.actions import Report
from pqn_whobot.actions import Scope
from pqn_whobot.actions import Section
from pqn_whobot.actions import Status
from pqn_whobot.actions import action
from pqn_whobot.actions import decode
from pqn_whobot.actions import encode
from pqn_whobot.actions import prefill
from pqn_whobot.actions import scan_actions
from pqn_whobot.registry import Node

OK = Report(status=Status.OK, title="fine")

A_NODE = Node(api_url="http://node-a.invalid:9000", name="uiuc-public-left", reachable=True)

SLACK_OPTION_VALUE_LIMIT = 75
"""Slack's cap on a dropdown option's ``value`` — the tightest slot a payload rides in."""


# --------------------------------------------------------------------------------------
# The result vocabulary.
# --------------------------------------------------------------------------------------


def test_skipped_is_distinct_from_warn() -> None:
    """Did-not-run and ran-and-looked-wrong are different answers to different questions."""
    assert Status.SKIPPED is not Status.WARN
    assert {status.value for status in Status} == {"ok", "warn", "fail", "skipped", "on", "off"}


def test_the_state_pair_is_not_an_outcome() -> None:
    """A row read as "is this on" must not be glyphed with an outcome, or it contradicts itself."""
    assert Status.ON is not Status.OK
    assert Status.OFF is not Status.FAIL


def test_a_field_is_a_measurement_unless_it_carries_a_status() -> None:
    """The rule the renderer branches on, stated once here so it cannot drift silently."""
    assert Field(name="S", value="2.4142").status is None
    assert Field(name="Router", value="12ms", status=Status.OK).status is Status.OK


def test_a_section_needs_nothing_but_what_it_means() -> None:
    empty = Section()
    assert empty.label is None
    assert empty.fields == []
    assert empty.note is None
    assert empty.error is None


def test_reports_do_not_share_mutable_defaults() -> None:
    """A default_factory slip here would have one Report's sections appear in the next."""
    first = Report(status=Status.OK, title="First")
    second = Report(status=Status.OK, title="Second")
    first.sections.append(Section(label="only mine"))
    first.notes.append("only mine")
    assert second.sections == []
    assert second.notes == []


def test_a_report_states_only_status_and_title_at_minimum() -> None:
    report = Report(status=Status.OK, title="List Nodes")
    assert report.summary is None
    assert report.image is None
    assert report.sections == []


# --------------------------------------------------------------------------------------
# The scan: what it accepts.
# --------------------------------------------------------------------------------------


class Declarer:
    """A stand-in for a Whobot subclass, carrying one Action of each shape."""

    @action(label="List Nodes", description="Every Node, with its reachability.")
    async def list_nodes(self) -> Report:
        return OK

    @action(label="Node Info", scope=Scope.ONE)
    async def node_info(self, node: Node) -> Report:
        return Report(status=Status.OK, title=node.api_url)

    @action(label="Change Game availability", scope=Scope.ONE, timeout_s=30.0)
    async def set_availability(self, node: Node, *, chsh: bool = True, qf: bool = False, ssm: bool = True) -> Report:
        return Report(status=Status.OK, title=f"{node.api_url} {chsh} {qf} {ssm}")

    @prefill(set_availability)
    async def _availability_prefill(self, node: Node) -> dict[str, object]:
        return {"chsh": False, "qf": True, "ssm": False}

    @action(label="Reboot", scope=Scope.ONE, destructive=True, timeout_s=360.0)
    async def reboot(self, node: Node) -> Report:
        return OK

    async def a_helper(self, node: Node) -> Report:
        """Undecorated, so it must never appear in the menu."""
        return OK


def test_the_scan_finds_only_marked_methods() -> None:
    """Opt-in is what stops adding a helper from accidentally adding a button in Slack."""
    found = scan_actions(Declarer)
    assert set(found) == {"list_nodes", "node_info", "set_availability", "reboot"}


def test_an_action_takes_its_name_from_the_method_and_its_text_from_the_decorator() -> None:
    act = scan_actions(Declarer)["list_nodes"]
    assert act.name == "list_nodes"
    assert act.label == "List Nodes"
    assert act.description == "Every Node, with its reachability."
    assert act.scope is Scope.NONE
    assert act.destructive is False


def test_declaration_defaults_are_non_destructive_and_whobot_level() -> None:
    """The safe values are the ones an author gets without asking for them."""
    act = scan_actions(Declarer)["list_nodes"]
    assert act.scope is Scope.NONE
    assert act.destructive is False
    assert act.timeout_s == pytest.approx(120.0)


def test_the_node_argument_is_not_a_form_parameter() -> None:
    """A Node is chosen from a dropdown, so it must not also be asked for in the form."""
    assert scan_actions(Declarer)["node_info"].parameters == ()


def test_parameters_are_parsed_from_the_signature_with_its_defaults() -> None:
    """There is no second parameter declaration, which is why none can fall out of step."""
    parameters = scan_actions(Declarer)["set_availability"].parameters
    assert [p.name for p in parameters] == ["chsh", "qf", "ssm"]
    assert [p.default for p in parameters] == [True, False, True]
    assert {p.annotation for p in parameters} == {bool}


def test_a_bool_without_a_signature_default_starts_unticked() -> None:
    class NoDefault:
        @action(label="Toggle")
        async def toggle(self, *, flag: bool) -> Report:
            return OK

    assert scan_actions(NoDefault)["toggle"].parameters[0].default is False


def test_a_subclass_inherits_its_parents_actions() -> None:
    """WhobotSlack declares no Actions; every one it serves is found on the base."""

    class Subclass(Declarer):
        @action(label="Extra")
        async def extra(self) -> Report:
            return OK

    found = scan_actions(Subclass)
    assert "extra" in found
    assert "list_nodes" in found


def test_a_prefill_is_attached_to_the_action_it_names() -> None:
    found = scan_actions(Declarer)
    assert found["set_availability"].prefill_name == "_availability_prefill"
    assert found["node_info"].prefill_name is None


# --------------------------------------------------------------------------------------
# The scan: what it rejects, and with what message.
# --------------------------------------------------------------------------------------


def test_scope_one_without_a_node_parameter_is_rejected() -> None:
    class Bad:
        @action(label="Bad", scope=Scope.ONE)
        async def bad(self) -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match=r"'bad'.*scope=ONE requires"):
        scan_actions(Bad)


def test_scope_one_with_a_node_that_is_not_a_node_is_rejected() -> None:
    class Bad:
        @action(label="Bad", scope=Scope.ONE)
        async def bad(self, node: str) -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match="must be annotated Node"):
        scan_actions(Bad)


def test_scope_none_taking_a_node_is_rejected() -> None:
    """The opposite mismatch, and the one that would silently never be passed a Node."""

    class Bad:
        @action(label="Bad")
        async def bad(self, node: Node) -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match="scope=NONE must not take a 'node'"):
        scan_actions(Bad)


def test_a_parameter_type_with_no_widget_is_rejected_by_name() -> None:
    """The message must name the Action and the parameter, or it is not actionable."""

    class Bad:
        @action(label="Bad")
        async def bad(self, *, reason: str = "") -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match=r"'bad'.*'reason'.*no widget"):
        scan_actions(Bad)


def test_an_unannotated_parameter_is_rejected() -> None:
    class Bad:
        @action(label="Bad")
        async def bad(self, *, flag=True) -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match="no type annotation"):
        scan_actions(Bad)


def test_a_positional_form_parameter_is_rejected() -> None:
    """Every Action is called as method(node, **params), so a positional one is a fiction."""

    class Bad:
        @action(label="Bad")
        # The positional bool ruff objects to is the whole point of this case: the scan
        # must reject it too, so that the rule is enforced rather than merely linted.
        async def bad(self, flag: bool = True) -> Report:  # noqa: FBT001, FBT002
            return OK

    with pytest.raises(ActionDeclarationError, match="must be keyword-only"):
        scan_actions(Bad)


def test_varargs_are_rejected() -> None:
    class Bad:
        @action(label="Bad")
        async def bad(self, *flags: bool) -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match="which no form can ask for"):
        scan_actions(Bad)


def test_a_synchronous_action_is_rejected() -> None:
    """Every Action is awaited, so a plain def would return a coroutine-shaped nothing."""

    class Bad:
        @action(label="Bad")
        def bad(self) -> Report:
            return OK

    with pytest.raises(ActionDeclarationError, match="must be 'async def'"):
        scan_actions(Bad)


def test_an_action_with_no_return_annotation_is_rejected() -> None:
    class Bad:
        @action(label="Bad")
        async def bad(self):
            return OK

    with pytest.raises(ActionDeclarationError, match="no return annotation"):
        scan_actions(Bad)


def test_an_action_returning_something_other_than_a_result_is_rejected() -> None:
    """The one rule that keeps Block Kit out: an Action returns the neutral type or nothing."""

    class Bad:
        @action(label="Bad")
        async def bad(self) -> dict[str, str]:
            return {}

    with pytest.raises(ActionDeclarationError, match="must return an ActionResult"):
        scan_actions(Bad)


def test_a_prefill_pointing_at_a_non_action_is_rejected() -> None:
    class Bad:
        async def not_an_action(self) -> Report:
            return OK

        @prefill(not_an_action)
        async def _fill(self) -> dict[str, object]:
            return {}

    with pytest.raises(ActionDeclarationError, match=r"'not_an_action'.*not an Action"):
        scan_actions(Bad)


def test_a_synchronous_prefill_is_rejected() -> None:
    class Bad:
        @action(label="Bad")
        async def bad(self, *, flag: bool = True) -> Report:
            return OK

        @prefill(bad)
        def _fill(self) -> dict[str, object]:
            return {}

    with pytest.raises(ActionDeclarationError, match=r"prefill.*must be 'async def'"):
        scan_actions(Bad)


def test_two_prefills_for_one_action_are_rejected() -> None:
    """Ambiguous, and the loser would be picked by member ordering — a silent coin toss."""

    class Bad:
        @action(label="Bad")
        async def bad(self, *, flag: bool = True) -> Report:
            return OK

        @prefill(bad)
        async def _fill_a(self) -> dict[str, object]:
            return {}

        @prefill(bad)
        async def _fill_b(self) -> dict[str, object]:
            return {}

    with pytest.raises(ActionDeclarationError, match="more than one prefill"):
        scan_actions(Bad)


# --------------------------------------------------------------------------------------
# Calling and parameter coercion.
# --------------------------------------------------------------------------------------


def test_calling_looks_the_method_up_on_the_owner() -> None:
    act = scan_actions(Declarer)["node_info"]
    result = asyncio.run(act.call(Declarer(), A_NODE, None))
    assert isinstance(result, Report)
    assert result.title == A_NODE.api_url


def test_a_prefill_is_resolved_on_the_owner() -> None:
    act = scan_actions(Declarer)["set_availability"]
    assert act.prefill_name is not None
    assert asyncio.run(act.prefill_values(Declarer(), A_NODE)) == {"chsh": False, "qf": True, "ssm": False}


def test_coercing_fills_in_every_declared_parameter() -> None:
    act = scan_actions(Declarer)["set_availability"]
    assert act.coerce_params({"chsh": False}) == {"chsh": False, "qf": False, "ssm": True}


def test_coercing_drops_a_parameter_the_action_no_longer_declares() -> None:
    """A modal can outlive the deploy that renamed a parameter; that must not raise."""
    act = scan_actions(Declarer)["set_availability"]
    coerced = act.coerce_params({"chsh": True, "removed_last_week": True})
    assert "removed_last_week" not in coerced
    assert coerced["chsh"] is True


def test_coercing_nothing_yields_the_declared_defaults() -> None:
    act = scan_actions(Declarer)["set_availability"]
    assert act.coerce_params(None) == {"chsh": True, "qf": False, "ssm": True}


def test_coercing_an_action_with_no_parameters_yields_nothing() -> None:
    act = scan_actions(Declarer)["node_info"]
    assert act.coerce_params({"stale": True}) == {}


# --------------------------------------------------------------------------------------
# The payload codec.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pending",
    [
        PendingInvocation(),
        PendingInvocation(action="list_nodes"),
        PendingInvocation(action="node_info", node_url="http://node-b.invalid:9000"),
        PendingInvocation(action="reboot", node_url="http://node-b.invalid:9000", confirmed=True),
        PendingInvocation(
            action="set_availability",
            node_url="http://node-b.invalid:9000",
            params={"chsh": True, "qf": False, "ssm": True},
            confirmed=False,
        ),
    ],
)
def test_a_payload_round_trips(pending: PendingInvocation) -> None:
    assert decode(encode(pending)) == pending


def test_nothing_chosen_yet_encodes_as_an_empty_payload() -> None:
    """Opening the menu is the same code path as every other click, carrying no state."""
    assert encode(PendingInvocation()) == "{}"


def test_the_encoding_omits_what_has_not_been_chosen() -> None:
    """Every byte spent on a null is a byte the 75-character dropdown slot does not have."""
    encoded = encode(PendingInvocation(action="node_info"))
    assert json.loads(encoded) == {"a": "node_info"}


def test_a_dropdown_payload_fits_slacks_seventy_five_character_option_value() -> None:
    """The tightest slot carries the least: params never ride in a dropdown option."""
    longest = PendingInvocation(action="set_availability", node_url="http://node-b.invalid:9000")
    assert len(encode(longest)) <= SLACK_OPTION_VALUE_LIMIT


def test_decoding_something_that_is_not_json_is_rejected() -> None:
    with pytest.raises(PayloadError, match="not a Whobot payload"):
        decode("this is not json")


def test_decoding_json_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(PayloadError, match="expected an object"):
        decode("[1, 2, 3]")


def test_decoding_a_payload_whose_parameters_are_not_an_object_is_rejected() -> None:
    with pytest.raises(PayloadError, match="parameters must be an object"):
        decode('{"a":"set_availability","p":"chsh"}')


def test_decoding_does_not_judge_whether_the_action_still_exists() -> None:
    """Staleness is dispatch's question — it re-renders. The codec only reads."""
    assert decode('{"a":"deleted_last_week"}') == PendingInvocation(action="deleted_last_week")
