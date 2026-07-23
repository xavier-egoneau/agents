from datetime import UTC, datetime

from pydantic_ai import FunctionToolset


def utc_now(justification: str = "") -> str:
    """Return the current UTC date and time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class DateTimeModule:
    def toolsets(self):
        return [FunctionToolset(tools=[utc_now])]

    def instructions(self):
        return []

    def capabilities(self):
        return []


module = DateTimeModule()
