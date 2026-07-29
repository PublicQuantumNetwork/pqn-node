"""``pqn_whobot`` may import from ``pqn_node``; nothing in ``pqn_node`` may import from ``pqn_whobot``.

Checked by parsing imports rather than grepping, so a mention in a docstring or comment
can't fail the test and a deferred import can't sneak past it.
"""

import ast
from pathlib import Path

import pqn_node
import pqn_whobot

NODE_ROOT = Path(pqn_node.__file__).parent
WHOBOT_PACKAGE = pqn_whobot.__name__


def _imported_modules(source: Path) -> set[str]:
    """Every module name imported by a file, including inside functions and `if TYPE_CHECKING`."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported.add(node.module)
    return imported


def test_pqn_node_never_imports_pqn_whobot() -> None:
    offenders = {
        source.relative_to(NODE_ROOT).as_posix()
        for source in NODE_ROOT.rglob("*.py")
        if any(module.split(".")[0] == WHOBOT_PACKAGE for module in _imported_modules(source))
    }

    assert offenders == set(), f"pqn_node must not import pqn_whobot, but these files do: {sorted(offenders)}"


def test_the_boundary_check_can_actually_fail(tmp_path: Path) -> None:
    """Guard the guard: an import scanner that finds nothing would pass vacuously."""
    offender = tmp_path / "offender.py"
    offender.write_text("from pqn_whobot.registry import resolve_nodes\nimport pqn_whobot.config\n", encoding="utf-8")

    assert _imported_modules(offender) == {"pqn_whobot.registry", "pqn_whobot.config"}
