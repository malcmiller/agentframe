"""Service registration and per-run resolution for ``Inject[T]`` parameters."""

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from types import MappingProxyType
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

    def scope(self, stack: AsyncExitStack) -> "ServiceScope":
        """Return the services of one run, cleaned up when ``stack`` closes.

        Args:
            stack: Exit stack that lives exactly as long as the run.
        """
        return ServiceScope(
            MappingProxyType(self._singletons), MappingProxyType(self._scoped), stack
        )

    def _check_not_registered(self, service_type: object) -> None:
        if self.is_registered(service_type):
            raise DefinitionError(
                f"Service {_type_name(service_type)} is already registered"
            )


class ServiceScope:
    """The services of one run, shared by its skill and the tools it calls.

    Created by ``Services.scope``. Scoped services are created at most once
    per type for the run, even when concurrent ``ctx.generate`` calls resolve
    them at the same time. Their cleanup is registered on the run's exit
    stack; singletons are never closed.

    Args:
        singletons: Registered shared instances by type.
        scoped: Registered scoped factories by type.
        stack: Exit stack that lives exactly as long as the run.
    """

    def __init__(
        self,
        singletons: Mapping[object, object],
        scoped: Mapping[object, Callable[[], object]],
        stack: AsyncExitStack,
    ) -> None:
        self._singletons = singletons
        self._scoped = scoped
        self._stack = stack
        self._created: dict[object, object] = {}
        self._creating = asyncio.Lock()

    async def resolve(self, needed: Mapping[str, object]) -> dict[str, object]:
        """Return service instances keyed by parameter name.

        Args:
            needed: Parameter name to requested service type.

        Raises:
            DefinitionError: If a requested service type is not registered, or a
                generator factory does not yield exactly once.
        """
        resolved: dict[str, object] = {}
        for parameter_name, service_type in needed.items():
            if service_type in self._singletons:
                resolved[parameter_name] = self._singletons[service_type]
                continue
            if service_type not in self._scoped:
                raise DefinitionError(
                    f"Service {_type_name(service_type)} is not registered"
                )
            resolved[parameter_name] = await self._scoped_instance(service_type)
        return resolved

    async def _scoped_instance(self, service_type: object) -> object:
        # A factory may await, so without the lock two concurrent resolves
        # could both see the type missing and create it twice.
        async with self._creating:
            if service_type not in self._created:
                factory = self._scoped[service_type]
                self._created[service_type] = await _create_scoped(factory, self._stack)
            return self._created[service_type]


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
