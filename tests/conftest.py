"""Shared test fixtures."""

import pytest

from local_server import ServeLocally, serve_locally


@pytest.fixture
def serve() -> ServeLocally:
    """Return ``serve_locally`` for serving an ``AgentApp`` over localhost HTTP."""
    return serve_locally
