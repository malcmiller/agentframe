import asyncio
from collections.abc import AsyncIterator

import pytest

from agentframe import (
    Agent,
    DefinitionError,
    Inject,
    LimitExceededError,
    RunContext,
    RunLimits,
    Runtime,
    Services,
    agent,
    skill,
)


class Database:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class Session:
    pass


@agent(version="1.0.0")
class Store(Agent):
    """Uses injected services."""

    @skill(id="database")
    async def database(self, db: Inject[Database], ctx: RunContext) -> int:
        """Return the identity of the injected database."""
        return id(db)

    @skill(id="session")
    async def session(
        self, first: Inject[Session], second: Inject[Session], ctx: RunContext
    ) -> int:
        """Return the identity of the injected session."""
        assert first is second
        return id(first)

    @skill(id="fail")
    async def fail(self, db: Inject[Database], ctx: RunContext) -> None:
        """Always fail."""
        raise RuntimeError("boom")

    @skill(id="hang", limits=RunLimits(timeout_seconds=0.01))
    async def hang(self, db: Inject[Database], ctx: RunContext) -> None:
        """Never finish in time."""
        await asyncio.sleep(10)


async def test_singleton_is_injected_and_never_closed() -> None:
    database = Database()
    services = Services()
    services.add(Database, database)

    value = await Runtime(services=services).invoke(Store(), skill="database")

    assert value == id(database)
    assert not database.closed


async def test_sync_factory_creates_a_service() -> None:
    services = Services()
    services.add_scoped(Session, Session)

    assert await Runtime(services=services).invoke(Store(), skill="session")


async def test_async_factory_creates_a_service() -> None:
    async def open_database() -> Database:
        return Database()

    services = Services()
    services.add_scoped(Database, open_database)

    assert await Runtime(services=services).invoke(Store(), skill="database")


def generator_services() -> tuple[Services, list[Database]]:
    opened: list[Database] = []

    async def open_database() -> AsyncIterator[Database]:
        database = Database()
        opened.append(database)
        yield database
        await database.close()

    services = Services()
    services.add_scoped(Database, open_database)
    return services, opened


async def test_generator_cleanup_runs_after_success() -> None:
    services, opened = generator_services()

    await Runtime(services=services).invoke(Store(), skill="database")

    assert opened[0].closed


async def test_generator_cleanup_runs_after_skill_exception() -> None:
    services, opened = generator_services()

    with pytest.raises(RuntimeError, match="boom"):
        await Runtime(services=services).invoke(Store(), skill="fail")

    assert opened[0].closed


async def test_generator_cleanup_runs_after_timeout() -> None:
    services, opened = generator_services()

    with pytest.raises(LimitExceededError):
        await Runtime(services=services).invoke(Store(), skill="hang")

    assert opened[0].closed


async def test_each_run_gets_a_new_scoped_instance() -> None:
    services, opened = generator_services()
    runtime = Runtime(services=services)

    await runtime.invoke(Store(), skill="database")
    await runtime.invoke(Store(), skill="database")

    assert len(opened) == 2
    assert opened[0] is not opened[1]


def test_duplicate_registration_is_rejected() -> None:
    services = Services()
    services.add(Database, Database())

    with pytest.raises(DefinitionError, match="already registered"):
        services.add_scoped(Database, Database)


async def test_missing_service_is_rejected_before_the_skill_runs() -> None:
    calls: list[str] = []

    @agent(version="1.0.0")
    class Tracker(Agent):
        """Records calls."""

        @skill(id="track")
        async def track(self, db: Inject[Database], ctx: RunContext) -> None:
            """Record a call."""
            calls.append("ran")

    with pytest.raises(DefinitionError, match="'db'"):
        await Runtime().invoke(Tracker(), skill="track")

    assert calls == []


def test_injected_parameters_are_not_arguments() -> None:
    database = Store.definition().skills["database"]

    assert database.argument_names == ()
    assert database.injected == {"db": Database}
