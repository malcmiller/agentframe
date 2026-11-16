"""A stdlib JSON log formatter for ``agentframe run --log-format json``.

Each record becomes one JSON object per line, so log pipelines can index a
run's correlation IDs without parsing message text.
"""

import json
import logging
from datetime import UTC, datetime

# The run correlation fields AgentFrame's hosted runs put in ``logging``
# ``extra``; see ``RunCorrelation.log_extra``. Other extras are left out, so
# a record never carries more than its message and these IDs.
CORRELATION_FIELDS = (
    "agent",
    "invocation_id",
    "root_invocation_id",
    "parent_invocation_id",
)


class JSONFormatter(logging.Formatter):
    """Formats each log record as one line of JSON.

    The object has ``time`` (UTC, ISO 8601), ``level``, ``logger`` and
    ``message``, then whichever of ``agent``, ``invocation_id``,
    ``root_invocation_id`` and ``parent_invocation_id`` the record carries,
    and ``exception`` / ``stack`` when it has a traceback or stack.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return ``record`` as a single line of JSON."""
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in CORRELATION_FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str)
