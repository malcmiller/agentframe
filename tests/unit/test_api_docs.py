"""Every public name has its own heading in the docs/api reference."""

import importlib
import pkgutil
import re
from pathlib import Path
from types import ModuleType

import agentframe

API_DOCS = Path(__file__).parents[2] / "docs" / "api"

# A heading names the code span it starts with, without a leading "@" or a
# trailing call or subscript: "## `@skill`", "## `dev_settings()`" and
# "## `Inject[T]`" document skill, dev_settings and Inject.
HEADING_NAME = re.compile(r"^#{2,} `@?([\w.]+)", re.MULTILINE)


def documented_names() -> set[str]:
    pages = API_DOCS.glob("*.md")
    return {name for page in pages for name in HEADING_NAME.findall(page.read_text())}


def public_modules() -> list[ModuleType]:
    """``agentframe`` and every subpackage that declares its own ``__all__``."""
    modules = [agentframe]
    for info in pkgutil.iter_modules(agentframe.__path__, "agentframe."):
        if info.ispkg:
            module = importlib.import_module(info.name)
            if hasattr(module, "__all__"):
                modules.append(module)
    return modules


def test_evaluation_exports_are_checked() -> None:
    assert "agentframe.evaluation" in [m.__name__ for m in public_modules()]


def test_every_export_has_an_api_heading() -> None:
    documented = documented_names()
    missing = [
        f"{module.__name__}.{name}"
        for module in public_modules()
        for name in module.__all__
        if name not in documented
    ]
    assert missing == [], "Give each a heading in docs/api: " + ", ".join(missing)
