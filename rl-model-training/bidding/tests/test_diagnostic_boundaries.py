"""The separation ``use_cases/README.md`` promises, enforced rather than trusted.

Both directions matter. Diagnostic code reaching into bed models would inherit semantics that
were never valid for a scanner; shared infrastructure reaching into a use case would make the
next resource family a rewrite instead of a directory.

These read the import graph statically, so a violation fails here rather than at the point
someone notices a scanner auction has grown a care ladder.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

DIAGNOSTIC = pathlib.Path("allocation/use_cases/diagnostic_machine")
SHARED = pathlib.Path("allocation")

#: Bed semantics. Every one of these is a thing a scanner does not have.
FORBIDDEN_IN_DIAGNOSTIC = (
    "allocation.use_cases.bed.profiles",
    "allocation.pathway",
    "allocation.sim",
    "allocation.ingest",
    "allocation.utility.components",
    "allocation.utility.engine",
)

#: Names that carry bed meaning even when imported from a shared module.
FORBIDDEN_NAMES = ("HospitalState", "QAction", "PathwayPlan", "PathwayOptions", "CareNeed")


def imports(path: pathlib.Path) -> list[tuple[str, tuple[str, ...]]]:
    """Every import in one file, as ``(module, imported names)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, tuple(a.name for a in node.names)))
        elif isinstance(node, ast.Import):
            out.extend((a.name, ()) for a in node.names)
    return out


def diagnostic_modules() -> list[pathlib.Path]:
    return sorted(DIAGNOSTIC.glob("*.py"))


#: The composition root: the only place allowed to name a use case. Registering the resource
#: families has to happen somewhere, and the alternative — a core layer importing one family —
#: is precisely what this module exists to forbid. Everything below these three is a core
#: layer and stays use-case agnostic.
COMPOSITION_ROOT = {"api", "cli.py", "__main__.py"}


def shared_modules() -> list[pathlib.Path]:
    return sorted(
        path
        for path in SHARED.rglob("*.py")
        if "use_cases" not in path.parts
        and "__pycache__" not in path.parts
        and not (COMPOSITION_ROOT & set(path.relative_to(SHARED).parts))
    )


@pytest.mark.parametrize("path", diagnostic_modules(), ids=lambda p: p.name)
def test_diagnostic_code_imports_no_bed_models(path):
    for module, _ in imports(path):
        assert not any(
            module == forbidden or module.startswith(forbidden + ".")
            for forbidden in FORBIDDEN_IN_DIAGNOSTIC
        ), f"{path.name} imports bed model {module}"


@pytest.mark.parametrize("path", diagnostic_modules(), ids=lambda p: p.name)
def test_diagnostic_code_imports_no_bed_shaped_names(path):
    for _, names in imports(path):
        overlap = set(names) & set(FORBIDDEN_NAMES)
        assert not overlap, f"{path.name} imports bed-shaped {sorted(overlap)}"


@pytest.mark.parametrize("path", shared_modules(), ids=lambda p: str(p))
def test_shared_infrastructure_does_not_import_a_use_case(path):
    """Shared code that knows about one family cannot be shared with the next.

    Scoped to the core layers. ``api/``, ``cli.py`` and ``__main__.py`` are the composition
    root (see :data:`COMPOSITION_ROOT`) and are expected to import the families they serve.
    """
    for module, _ in imports(path):
        assert not module.startswith("allocation.use_cases"), (
            f"{path} imports {module}; shared infrastructure must not depend on a use case"
        )


def test_diagnostic_code_does_reuse_the_shared_core():
    """The other half of the claim: reuse, not a parallel stack.

    Named explicitly rather than counted, because "imports something from auction/" would pass
    on a module that imported one constant and reimplemented everything else.
    """
    reused = {
        module
        for path in diagnostic_modules()
        for module, _ in imports(path)
        if module.startswith(("allocation.auction", "allocation.budget", "allocation.policy",
                              "allocation.utility", "allocation.config"))
    }
    for expected in (
        "allocation.auction.guards",      # the bid clamps
        "allocation.auction.reserve",     # the closing price
        "allocation.auction.settle",      # the winner rule and the charge
        "allocation.auction.state",       # positions and the standing-bid view
        "allocation.budget.ledger",       # opening and charging a shift
        "allocation.budget.spend",        # contention and cost
    ):
        assert expected in reused, f"diagnostic code no longer reuses {expected}"
