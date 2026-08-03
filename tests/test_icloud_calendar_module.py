from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_module():
    path = Path(__file__).parents[1] / "tools" / "modules" / "icloud_calendar" / "module.py"
    spec = importlib.util.spec_from_file_location("test_amk_icloud_calendar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_icalendar_events_are_unfolded_and_parsed() -> None:
    module = load_module()
    content = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:event-1\r
SUMMARY:Point hebdo\r
DTSTART:20260804T080000Z\r
DTEND:20260804T083000Z\r
DESCRIPTION:Première ligne\\nDeuxième ligne très \r
 longue\r
END:VEVENT\r
END:VCALENDAR\r
"""

    assert module._parse_events(content, "Travail") == [
        {
            "calendar": "Travail",
            "uid": "event-1",
            "summary": "Point hebdo",
            "dtstart": "2026-08-04T08:00:00+00:00",
            "dtend": "2026-08-04T08:30:00+00:00",
            "description": "Première ligne\nDeuxième ligne très longue",
        }
    ]


def test_discovered_urls_cannot_exfiltrate_icloud_credentials() -> None:
    module = load_module()

    assert module._icloud_url("https://caldav.icloud.com/", "/123/calendars/") == (
        "https://caldav.icloud.com/123/calendars/"
    )
    try:
        module._icloud_url("https://caldav.icloud.com/", "https://attacker.example/calendar")
    except ValueError as exc:
        assert "hors du domaine" in str(exc)
    else:
        raise AssertionError("an external discovered URL must be rejected")


async def test_missing_credentials_are_reported_by_secret_name() -> None:
    module = load_module()
    ctx = SimpleNamespace(deps=SimpleNamespace(secret_resolver=lambda _name: None))

    result = await module.icloud_list_calendars(ctx)

    assert result["ok"] is False
    assert result["error"]["type"] == "caldav"
    assert "ICLOUD_CALDAV_USERNAME" in result["error"]["message"]
    assert "ICLOUD_CALDAV_PASSWORD" in result["error"]["message"]
