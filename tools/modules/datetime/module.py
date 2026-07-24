from datetime import UTC, datetime

from pydantic_ai import FunctionToolset


def utc_now(justification: str = "") -> dict:
    """Return the current UTC date and time as an ISO-8601 string."""
    value = datetime.now(UTC).isoformat()
    return {
        "ok": True,
        "data": {"utc": value},
        "error": None,
        "metadata": {},
        "utc": value,
    }


class DateTimeModule:
    def toolsets(self):
        return [FunctionToolset(tools=[utc_now])]

    def instructions(self):
        return []

    def capabilities(self):
        return []


module = DateTimeModule()
