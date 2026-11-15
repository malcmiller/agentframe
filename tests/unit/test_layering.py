"""Lower layers never import the hosting layer, and shared leaves stay leaves.

Importing any ``agentframe`` module loads the whole package, so these checks
read the import statements instead of importing the modules.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parents[2] / "src" / "agentframe"
BELOW_HOSTING = [
    "authoring",
    "discovery",
    "evaluation",
    "models",
    "orchestration",
    "runtime",
    "security",
]
LEAVES = ["errors.py", "skill_call.py", "urls.py", "usage.py"]


def imported_modules(path: Path) -> set[str]:
    """Return every ``agentframe`` module that ``path`` imports, anywhere in it.

    Relative imports are resolved, and ``from agentframe import hosting``
    counts as importing ``agentframe.hosting``.
    """
    package = ["agentframe", *path.parent.relative_to(PACKAGE).parts]
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            if is_package(module):
                modules.update(f"{module}.{alias.name}" for alias in node.names)
            else:
                modules.add(module)
    return {name for name in modules if name.startswith("agentframe")}


def is_package(module: str) -> bool:
    """Return whether ``module`` names a package (a folder) of agentframe."""
    if module != "agentframe" and not module.startswith("agentframe."):
        return False
    return PACKAGE.joinpath(*module.split(".")[1:]).is_dir()


@pytest.mark.parametrize("group", BELOW_HOSTING)
def test_lower_layers_do_not_import_hosting(group: str) -> None:
    for path in sorted((PACKAGE / group).glob("*.py")):
        hosting = {
            name
            for name in imported_modules(path)
            if name.startswith("agentframe.hosting")
        }
        assert not hosting, f"{path.name} imports {sorted(hosting)}"


@pytest.mark.parametrize("leaf", LEAVES)
def test_shared_leaves_import_only_other_leaves(leaf: str) -> None:
    allowed = {"agentframe." + name.removesuffix(".py") for name in LEAVES}

    assert imported_modules(PACKAGE / leaf) <= allowed
