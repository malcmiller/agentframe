import asyncio
import re
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import agentframe

ROOT = Path(__file__).parents[2]
REPOSITORY_PATH = re.compile(
    r"https://github\.com/malcmiller/agentframe/(?:blob|tree)/main/([^#]+)"
)


def test_package_imports() -> None:
    assert agentframe.__version__ == "0.2.0"
    assert version("agentframe-py") == agentframe.__version__


def test_package_ships_type_marker() -> None:
    assert files("agentframe").joinpath("py.typed").is_file()


async def test_async_tests_run() -> None:
    result = await asyncio.sleep(0, result="async tests work")
    assert result == "async tests work"


def test_readme_links_work_on_pypi() -> None:
    # PyPI shows README.md without the repository, so a relative link breaks.
    targets = re.findall(r"\]\(([^)\s]+)\)", (ROOT / "README.md").read_text())
    relative = [t for t in targets if not t.startswith(("https://", "http://", "#"))]
    assert relative == []
    linked = [m.group(1) for t in targets if (m := REPOSITORY_PATH.match(t))]
    assert linked
    assert [path for path in linked if not (ROOT / path).exists()] == []
