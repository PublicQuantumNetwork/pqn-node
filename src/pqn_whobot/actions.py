"""The Action contract: how an Action is declared, what it is passed, and what it returns.

The types and machinery the Actions in ``whobot.py`` are built from. Nothing here is an
Action, and nothing here knows about a Chat Platform.

Four things live here, in this order:

* **The result vocabulary** — ``Status``, ``Field``, ``Section`` and ``Report``. An Action
  returns one of these to describe *what happened*; a Chat Platform subclass decides what
  that looks like, so platform markup never belongs in one.
* **Declaring an Action** — ``@action`` and ``@prefill``, which mark a method without
  wrapping it; ``Scope``, ``ActionMeta`` and ``Parameter``, which record what a declaration
  says; and ``Action``, one declared Action ready to run. ``Action.call`` invokes it on the
  instance it is passed with the arguments its scope implies, and ``coerce_params`` turns a
  payload's form values into those arguments.
* **The scan** — ``scan_actions`` finds the marked methods on a class, parses each signature
  once into ``Parameter``s, and validates. It runs while the class is being created, so a
  malformed Action fails at import rather than in front of an operator mid-click.
* **The payload** — ``PendingInvocation`` and its codec. A Chat Platform tells you nothing
  between one click and the next except the string written into the widget rendered last, so
  the payload holds the whole of the interaction state. ``ReplyHandle`` is here too: the
  opaque marker saying where a reply goes, which each Chat Platform subclasses.
"""

import inspect
import json
import logging
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace
from enum import StrEnum
from typing import Any
from typing import TypeVar

from pqn_whobot.registry import Node

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# The result vocabulary: what an Action returns.
# --------------------------------------------------------------------------------------


class Status(StrEnum):
    """What a ``Field`` or ``Section`` says about itself.

    ``OK``, ``WARN``, ``FAIL`` and ``SKIPPED`` are outcomes — how something turned out.
    ``SKIPPED`` is not ``WARN``: one means it did not run, the other that it ran and looked
    wrong.

    ``ON`` and ``OFF`` are states rather than outcomes, for a row answering "is this thing
    on". Neither is bad news, so neither affects ``overall``, and a Chat Platform marks them
    with something other than a tick or a cross.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"
    ON = "on"
    OFF = "off"

    @classmethod
    def overall(cls, statuses: Iterable["Status | None"]) -> "Status":
        """Reduce a group of statuses to the one a summary should show.

        ``FAIL`` wins, then ``WARN``, and anything else gives ``OK``. This is a bad-news
        precedence and not an ordering: ``SKIPPED``, ``ON``, ``OFF`` and ``None`` are ranked
        neither against each other nor against ``OK``. ``None`` is accepted so that callers
        need not filter out their status-less Fields first.
        """
        seen = set(statuses)
        for candidate in (cls.FAIL, cls.WARN):
            if candidate in seen:
                return candidate
        return cls.OK


@dataclass(frozen=True)
class Field:
    """One named value in a result.

    ``status`` also selects the layout, so that no presentation flag is needed: a Field with
    one is a checklist entry and renders as a line, and a Field without one is a measurement
    and renders in a grid beside its neighbours.
    """

    name: str
    value: str
    status: Status | None = None


@dataclass(frozen=True)
class Section:
    """A group of Fields under an optional heading.

    ``note`` is a short footer, such as an elapsed time. ``error`` is preformatted text,
    such as a traceback, and is the one field exempt from the no-markup rule — a traceback's
    punctuation is not Whobot's to sanitise.
    """

    label: str | None = None
    status: Status | None = None
    fields: list[Field] = dataclass_field(default_factory=list)
    note: str | None = None
    error: str | None = None


class ActionResult:
    """Marker base class for anything an Action may return.

    A Chat Platform registers one renderer per concrete subclass and dispatches on the type.
    A test asserts every subclass has a renderer, so an unrenderable result fails the suite
    rather than an operator's click.
    """


@dataclass(frozen=True)
class Report(ActionResult):
    """The general-purpose result: a status, a title, and a flat list of Sections.

    Return this unless a result needs a shape it cannot express. The Daily Digest is the one
    Action that does, because Node x (hardware checklist + Games) nests one level deeper.
    """

    status: Status
    title: str
    summary: str | None = None
    sections: list[Section] = dataclass_field(default_factory=list)
    image: bytes | None = None
    notes: list[str] = dataclass_field(default_factory=list)


# --------------------------------------------------------------------------------------
# Declaring an Action.
# --------------------------------------------------------------------------------------


class ActionDeclarationError(TypeError):
    """An Action is declared wrongly. Raised while the class is being created, never later."""


class Scope(StrEnum):
    """What an Action acts on."""

    NONE = "none"
    """Whobot-level: no Node is chosen, and the method takes no ``node``."""

    ONE = "one"
    """One Node, chosen by the operator and passed as the method's first argument."""


ActionMethod = Callable[..., Awaitable[ActionResult]]
PrefillMethod = Callable[..., Awaitable[dict[str, object]]]

_ACTION_ATTR = "_whobot_action"
_PREFILL_ATTR = "_whobot_prefill"

DEFAULT_TIMEOUT_S = 120.0

WIDGET_TYPES: tuple[type, ...] = (bool,)
"""Parameter types the form generator can render. ``bool`` becomes a checkbox.

The scan rejects any other type by name, so an unsupported parameter fails at import with a
message rather than producing an empty form. Adding ``str``, ``int``/``float`` or
``Literal``/enum is one entry here and one branch in the Chat Platform's form renderer.
"""


@dataclass(frozen=True)
class Parameter:
    """One question the form asks, parsed from an Action's signature.

    ``default`` is always set: a checkbox is ticked or it is not, so a ``bool`` with no
    signature default starts unticked. A widget for which "no value" differs from a default
    will need a ``required`` flag here.
    """

    name: str
    annotation: type
    default: object


@dataclass(frozen=True)
class ActionMeta:
    """What ``@action`` records — only the things a signature cannot say itself."""

    label: str
    description: str | None = None
    scope: Scope = Scope.NONE
    destructive: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class Action:
    """A declared Action: its metadata, its parsed parameters, and the name of its method.

    Pure metadata, holding no callable. ``name`` and ``prefill_name`` are attribute names on
    the class that declared them, which ``call`` looks up on the owner it is passed — so the
    owner binds the method, as ordinary attribute access always would. One of these is
    therefore shared by every instance, with nothing to copy and nothing to rebind.
    """

    name: str
    label: str
    description: str | None
    scope: Scope
    destructive: bool
    timeout_s: float
    parameters: tuple[Parameter, ...]
    prefill_name: str | None = None

    async def call(self, owner: object, node: Node | None, params: dict[str, object] | None) -> ActionResult:
        """Run the Action against ``owner``, passing the Node only when its scope declares one.

        A ``Scope.ONE`` method takes the Node as its first argument and a ``Scope.NONE``
        method takes none. The scan enforces that, so the call shape follows from ``scope``.
        """
        arguments = (node,) if self.scope is Scope.ONE else ()
        method: ActionMethod = getattr(owner, self.name)
        return await method(*arguments, **self.coerce_params(params))

    async def prefill_values(self, owner: object, node: Node | None) -> dict[str, object]:
        """Ask the ``@prefill`` what the form should open on. Empty when there is none."""
        if self.prefill_name is None:
            return {}
        arguments = (node,) if self.scope is Scope.ONE else ()
        method: PrefillMethod = getattr(owner, self.prefill_name)
        return await method(*arguments)

    def coerce_params(self, raw: dict[str, object] | None) -> dict[str, object]:
        """Turn a payload's form values into this Action's keyword arguments.

        Unknown keys are dropped and missing ones take their default, because a payload can
        outlive the code that wrote it: a modal may have been rendered before a deploy
        renamed a parameter, and that must run the Action as declared today rather than
        raise ``TypeError`` inside the call.
        """
        supplied = raw or {}
        unknown = supplied.keys() - {parameter.name for parameter in self.parameters}
        if unknown:
            logger.warning("%s: ignoring parameters that no longer exist: %s", self.name, sorted(unknown))

        return {
            parameter.name: (
                parameter.annotation(supplied[parameter.name]) if parameter.name in supplied else parameter.default
            )
            for parameter in self.parameters
        }


F = TypeVar("F", bound=ActionMethod)
P = TypeVar("P", bound=PrefillMethod)


def action(
    *,
    label: str,
    description: str | None = None,
    scope: Scope = Scope.NONE,
    destructive: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Callable[[F], F]:
    """Mark a method as an Action, recording what its signature cannot say.

    The method is returned unchanged: this staples metadata onto it rather than wrapping it,
    so an Action stays an ordinary method and ``@prefill`` can link to it by identity.
    """

    def decorate(method: F) -> F:
        setattr(
            method,
            _ACTION_ATTR,
            ActionMeta(
                label=label,
                description=description,
                scope=scope,
                destructive=destructive,
                timeout_s=timeout_s,
            ),
        )
        return method

    return decorate


def prefill(target: ActionMethod) -> Callable[[P], P]:
    """Mark a method as the source of an Action's starting form values.

    ``target`` is the Action's function as written in the class body — the same object the
    scan finds, since ``@action`` does not wrap it. The link is therefore by identity and
    cannot be broken by a misspelt name.
    """

    def decorate(method: P) -> P:
        setattr(method, _PREFILL_ATTR, target)
        return method

    return decorate


# --------------------------------------------------------------------------------------
# The scan: turning a class into validated Actions as the class is created.
# --------------------------------------------------------------------------------------


def _fail(name: str, problem: str) -> ActionDeclarationError:
    """Build a declaration error naming the Action, so the message stands on its own."""
    return ActionDeclarationError(f"Action {name!r}: {problem}")


def _is_node_parameter(parameter: inspect.Parameter) -> bool:
    return parameter.name == "node" or parameter.annotation is Node


def _without_the_node(name: str, signature: inspect.Signature, scope: Scope) -> list[inspect.Parameter]:
    """Check the signature agrees with the scope about a Node, and return what remains.

    What remains is what the operator is asked for. The Node comes from a dropdown, so it
    must never also appear in the form.
    """
    declared = [p for p in signature.parameters.values() if p.name != "self"]

    if scope is Scope.ONE:
        if not declared or not _is_node_parameter(declared[0]):
            msg = "scope=ONE requires a first parameter 'node: Node'"
            raise _fail(name, msg)
        if declared[0].annotation is not Node:
            msg = f"parameter 'node' must be annotated Node, not {declared[0].annotation!r}"
            raise _fail(name, msg)
        return declared[1:]

    if any(_is_node_parameter(p) for p in declared):
        msg = "scope=NONE must not take a 'node' parameter; declare scope=Scope.ONE to act on one Node"
        raise _fail(name, msg)
    return declared


def _parse_parameters(name: str, signature: inspect.Signature, scope: Scope) -> tuple[Parameter, ...]:
    """Parse the parameters an operator supplies, rejecting any the form cannot ask for.

    They must be keyword-only. ``Action.call`` invokes every Action as
    ``method(node, **params)``, so a positional form parameter describes a call that never
    happens.
    """
    parameters = []
    for parameter in _without_the_node(name, signature, scope):
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            msg = f"parameter {parameter.name!r} is *args/**kwargs, which no form can ask for"
            raise _fail(name, msg)
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            msg = f"parameter {parameter.name!r} must be keyword-only; put a '*' before it"
            raise _fail(name, msg)
        if parameter.annotation is inspect.Parameter.empty:
            msg = f"parameter {parameter.name!r} has no type annotation, so no widget can be chosen for it"
            raise _fail(name, msg)
        if parameter.annotation not in WIDGET_TYPES:
            supported = ", ".join(widget.__name__ for widget in WIDGET_TYPES)
            msg = (
                f"parameter {parameter.name!r} is {parameter.annotation!r}, which has no widget; supported: {supported}"
            )
            raise _fail(name, msg)
        default = parameter.default if parameter.default is not inspect.Parameter.empty else parameter.annotation()
        parameters.append(Parameter(name=parameter.name, annotation=parameter.annotation, default=default))

    return tuple(parameters)


def _parse_action(name: str, method: ActionMethod, meta: ActionMeta) -> Action:
    """Validate one marked method against its metadata, and parse its form parameters."""
    if not inspect.iscoroutinefunction(method):
        msg = "must be 'async def' — every Action is awaited"
        raise _fail(name, msg)

    # eval_str resolves annotations that are strings, so a module using postponed
    # evaluation is compared against real types rather than against their spelling.
    signature = inspect.signature(method, eval_str=True)

    returns = signature.return_annotation
    if returns is inspect.Signature.empty:
        msg = "has no return annotation; an Action must declare it returns an ActionResult"
        raise _fail(name, msg)
    if not (isinstance(returns, type) and issubclass(returns, ActionResult)):
        msg = f"returns {returns!r}; an Action must return an ActionResult"
        raise _fail(name, msg)

    return Action(
        name=name,
        label=meta.label,
        description=meta.description,
        scope=meta.scope,
        destructive=meta.destructive,
        timeout_s=meta.timeout_s,
        parameters=_parse_parameters(name, signature, meta.scope),
    )


def _attach_prefills(
    cls: type,
    actions: dict[str, Action],
    members: list[tuple[str, Any]],
    by_function: dict[ActionMethod, str],
) -> dict[str, Action]:
    """Link each ``@prefill`` to the Action it points at, rejecting one that points nowhere.

    ``by_function`` is keyed by the Action's function because ``@prefill`` records its target
    by identity; what is stored on the Action is only the prefill's name.
    """
    attached = dict(actions)

    for name, method in members:
        target = getattr(method, _PREFILL_ATTR, None)
        if target is None:
            continue
        if not inspect.iscoroutinefunction(method):
            msg = f"{cls.__name__}: prefill {name!r} must be 'async def'"
            raise ActionDeclarationError(msg)
        action_name = by_function.get(target)
        if action_name is None:
            named = getattr(target, "__name__", target)
            msg = f"{cls.__name__}: prefill {name!r} points at {named!r}, which is not an Action"
            raise ActionDeclarationError(msg)
        if attached[action_name].prefill_name is not None:
            msg = f"{cls.__name__}: Action {action_name!r} has more than one prefill"
            raise ActionDeclarationError(msg)
        attached[action_name] = replace(attached[action_name], prefill_name=name)

    return attached


def scan_actions(cls: type) -> dict[str, Action]:
    """Find, validate and return every Action a class declares, inherited ones included.

    Called from ``__init_subclass__``, so it works on the class and produces metadata only.
    Being an Action is opt-in, so adding a helper method cannot turn it into a menu entry.
    """
    members = inspect.getmembers(cls, inspect.isfunction)
    marked = [
        (name, method, meta) for name, method in members if (meta := getattr(method, _ACTION_ATTR, None)) is not None
    ]
    actions = {name: _parse_action(name, method, meta) for name, method, meta in marked}
    return _attach_prefills(cls, actions, members, {method: name for name, method, _ in marked})


# --------------------------------------------------------------------------------------
# The payload: the only thing that survives between one click and the next.
# --------------------------------------------------------------------------------------


class PayloadError(ValueError):
    """A payload could not be decoded, which means something other than Whobot wrote it."""


class ReplyHandle:
    """Where a reply goes. An opaque marker, which ``Whobot`` passes around without opening.

    A Chat Platform subclasses it to carry whatever addressing it needs — ``WhobotSlack``
    carries a channel and a thread — so those reach the renderer without ``Whobot`` ever
    learning what a ``thread_ts`` is.
    """


@dataclass(frozen=True)
class PendingInvocation:
    """An interaction in progress: what has been chosen so far, and what has not.

    ``dispatch`` reads nothing else, so Whobot can restart between any two clicks and the
    next one still works. ``action=None`` means nothing has been chosen yet, so opening the
    menu needs no special case.
    """

    action: str | None = None
    node_url: str | None = None
    params: dict[str, object] | None = None
    confirmed: bool = False


# Short keys, because Slack's tightest slot is a dropdown option's 75-character value.
_KEY_ACTION = "a"
_KEY_NODE = "n"
_KEY_PARAMS = "p"
_KEY_CONFIRMED = "c"


def encode(pending: PendingInvocation) -> str:
    """Encode a pending invocation for a widget, omitting everything not yet chosen."""
    payload: dict[str, object] = {}
    if pending.action is not None:
        payload[_KEY_ACTION] = pending.action
    if pending.node_url is not None:
        payload[_KEY_NODE] = pending.node_url
    if pending.params is not None:
        payload[_KEY_PARAMS] = pending.params
    if pending.confirmed:
        payload[_KEY_CONFIRMED] = True
    return json.dumps(payload, separators=(",", ":"))


def decode(raw: str) -> PendingInvocation:
    """Decode what a widget sent back.

    This rejects only what is not a payload at all. Whether the Action still exists, or the
    Node is still registered, is ``dispatch``'s question — it re-renders for those.
    """
    try:
        payload = json.loads(raw)
    except ValueError as e:
        msg = f"not a Whobot payload: {e}"
        raise PayloadError(msg) from e
    if not isinstance(payload, dict):
        msg = f"not a Whobot payload: expected an object, got {type(payload).__name__}"
        raise PayloadError(msg)

    params = payload.get(_KEY_PARAMS)
    if params is not None and not isinstance(params, dict):
        msg = f"payload parameters must be an object, got {type(params).__name__}"
        raise PayloadError(msg)

    return PendingInvocation(
        action=payload.get(_KEY_ACTION),
        node_url=payload.get(_KEY_NODE),
        params=params,
        confirmed=bool(payload.get(_KEY_CONFIRMED, False)),
    )
