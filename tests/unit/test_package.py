import asyncio
from importlib.metadata import version
from importlib.resources import files

import agentframe


def test_package_imports() -> None:
    assert agentframe.__version__ == "0.1.0"
    assert version("agentframe") == agentframe.__version__


def test_package_ships_type_marker() -> None:
    assert files("agentframe").joinpath("py.typed").is_file()


async def test_async_tests_run() -> None:
    result = await asyncio.sleep(0, result="async tests work")
    assert result == "async tests work"
