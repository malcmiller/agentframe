"""Host configuration for serving an agent remotely."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from agentframe.auth import Authenticator
from agentframe.errors import DefinitionError


@dataclass(frozen=True)
class HostConfig:
    """Deployment settings for a remotely hosted agent.

    Only the fields needed to describe the host on its Agent Card exist so far;
    serving settings such as role mapping and resource ceilings come later.

    Args:
        public_url: The origin callers use to reach the agent, for example
            ``"https://echo.example.com"``. It is advertised on the Agent Card;
            it is not the address the server binds to.
        auth: How remote callers are authenticated. There is no default: use
            ``AllowAnonymous()`` to accept anonymous callers explicitly.

    Raises:
        DefinitionError: ``public_url`` is not an absolute http(s) URL, or it
            contains credentials, a query or a fragment.
    """

    public_url: str
    auth: Authenticator

    def __post_init__(self) -> None:
        # The URL is published on the public card, so credentials in it would
        # leak; the error message does not echo it for the same reason.
        parts = urlsplit(self.public_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or "@" in parts.netloc
            or parts.query
            or parts.fragment
        ):
            raise DefinitionError(
                "HostConfig public_url must be an absolute http(s) URL without "
                "credentials, query or fragment."
            )
