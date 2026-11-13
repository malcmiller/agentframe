"""The examples smoke runner, so it stays green between CI wheel builds."""

import os
from unittest import mock

from examples import smoke


async def test_every_example_returns_a_complete_validated_result() -> None:
    # The runner sets each example's environment variables; restore them after.
    with mock.patch.dict(os.environ):
        await smoke.main()
