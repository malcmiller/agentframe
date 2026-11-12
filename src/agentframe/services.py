"""Service registration and per-run resolution for ``Inject[T]`` parameters."""

import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from typing import Annotated, cast

from agentframe.errors import DefinitionError


class _InjectMarker:
    """Marks a skill parameter as an injected service."""


# Type checkers treat ``Inject[T]`` as ``T``. At runtime AgentFrame detects the
# parameter by checking ``typing.get_origin(annotation) is Inject``.
type Inject[T] = Annotated[T, _InjectMarker()]

type ScopedFactory[T] = (
    Callable[[], T] | Callable[[], Awaitable[T]] | Callable[[], AsyncIterator[T]]
)


class Services:
    """Registry of services that skills receive through ``Inject[T]``.

    Pass an instance to ``Runtime(services=...)``.
    """

    def __init__(self) -> None:
        self._singletons: dict[object, object] = {}
        self._scoped: dict[object, Callable[[], object]] = {}

    def add[T](self, service_type: type[T], instance: T) -> None:
        """Register an application-owned service shared by all runs.

        AgentFrame never closes this instance; the application owns its lifecycle.

        Args:
            service_type: Type that skills request with ``Inject[service_type]``.
            instance: The service instance to inject.

        Raises:
            DefinitionError: If ``service_type`` is already registered.
        """
        self._check_not_registered(service_type)
        self._singletons[service_type] = instance

    def add_scoped[T](self, service_type: type[T], factory: ScopedFactory[T]) -> None:
        """Register a service created freshly for each run.

        The factory may be a sync function, an async function, or an async
        generator function. For a generator, the code after its single ``yield``
        is cleanup and runs when the run ends, whether it succeeded or failed.

        Args:
            service_type: Type that skills request with ``Inject[service_type]``.
            factory: Zero-argument callable that creates the service.

        Raises:
            DefinitionError: If ``service_type`` is already registered.
        """
        self._check_not_registered(service_type)
        self._scoped[service_type] = factory

    def is_registered(self, service_type: object) -> bool:
        """Return whether ``service_type`` has been registered."""
        return service_type in self._singletons or service_type in self._scoped

    async def resolve(
        self, needed: Mapping[str, object], stack: AsyncExitStack
    ) -> dict[str, object]:
        """Return service instances for one run, keyed by parameter name.

        Scoped services are created once per type for the run. Their cleanup is
        registered on ``stack``; singletons are never closed.

        Args:
            needed: Parameter name to requested service type.
            stack: Exit stack that lives exactly as long as the run.

        Raises:
            DefinitionError: If a requested service type is not registered, or a
                generator factory does not yield exactly once.
        """
        created: dict[object, object] = {}
        resolved: dict[str, object] = {}
        for parameter_name, service_type in needed.items():
            if service_type in self._singletons:
                resolved[parameter_name] = self._singletons[service_type]
                continue
            if service_type not in self._scoped:
                raise DefinitionError(
                    f"Service {_type_name(service_type)} is not registered"
                )
            if service_type not in created:
                factory = self._scoped[service_type]
                created[service_type] = await _create_scoped(factory, stack)
            resolved[parameter_name] = created[service_type]
        return resolved

    def _check_not_registered(self, service_type: object) -> None:
        if self.is_registered(service_type):
            raise DefinitionError(
                f"Service {_type_name(service_type)} is already registered"
            )


async def _create_scoped(
    factory: Callable[[], object], stack: AsyncExitStack
) -> object:
    if inspect.isasyncgenfunction(factory):
        generator = factory()
        instance = await anext(generator)
        stack.push_async_callback(_finish_generator, generator)
        return instance
    if inspect.iscoroutinefunction(factory):
        return await cast(Callable[[], Awaitable[object]], factory)()
    return factory()


async def _finish_generator(generator: AsyncGenerator[object]) -> None:
    # Resume the generator instead of throwing the run's exception into it (as
    # contextlib.asynccontextmanager would), so the code after ``yield`` always
    # runs, like a pytest yield fixture.
    try:
        await anext(generator)
    except StopAsyncIteration:
        return
    await generator.aclose()
    raise DefinitionError("A scoped service factory must yield exactly once")


def _type_name(service_type: object) -> str:
    qualname = getattr(service_type, "__qualname__", None)
    # Keys without a __qualname__, such as generic aliases, have a useful repr.
    # noinspection PyStringConversionWithoutDunderMethod
    return qualname if isinstance(qualname, str) else repr(service_type)
