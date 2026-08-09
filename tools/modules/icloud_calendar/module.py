from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext

from agentic_kernel.network_policy import validate_http_target

ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"
USERNAME_SECRET = "ICLOUD_CALDAV_USERNAME"  # noqa: S105 - nom de secret, pas une valeur
PASSWORD_SECRET = "ICLOUD_CALDAV_PASSWORD"  # noqa: S105 - nom de secret, pas une valeur
DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"


def _failure(kind: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"type": kind, "message": message},
        "metadata": metadata,
    }


def _success(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": metadata}


def _credentials(ctx: RunContext[Any]) -> tuple[str, str]:
    resolver = getattr(ctx.deps, "secret_resolver", None)
    if not callable(resolver):
        raise ValueError("le résolveur de secrets du kernel est indisponible")
    username = resolver(USERNAME_SECRET)
    password = resolver(PASSWORD_SECRET)
    if not username or not password:
        missing = [
            name
            for name, value in ((USERNAME_SECRET, username), (PASSWORD_SECRET, password))
            if not value
        ]
        raise ValueError("secrets CalDAV manquants : " + ", ".join(missing))
    return username, password


def _icloud_url(base: str, href: str) -> str:
    target = urljoin(base, href)
    hostname = (urlparse(target).hostname or "").casefold()
    if hostname != "icloud.com" and not hostname.endswith(".icloud.com"):
        raise ValueError("iCloud a retourné une URL CalDAV hors du domaine icloud.com")
    return target


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: str,
    depth: str,
) -> httpx.Response:
    await validate_http_target(url)
    response = await client.request(
        method,
        url,
        headers={
            "Depth": depth,
            "Content-Type": "application/xml; charset=utf-8",
            "Accept": "application/xml, text/calendar",
        },
        content=body.encode("utf-8"),
    )
    if response.status_code not in {200, 207}:
        if response.status_code in {401, 403}:
            raise PermissionError(
                "authentification iCloud refusée ; vérifier l’identifiant Apple "
                "et le mot de passe spécifique à l’application"
            )
        raise httpx.HTTPStatusError(
            f"réponse CalDAV inattendue : HTTP {response.status_code}",
            request=response.request,
            response=response,
        )
    return response


def _first_text(root: ElementTree.Element, path: str) -> str | None:
    node = root.find(path)
    return node.text.strip() if node is not None and node.text else None


async def _discover_calendars(client: httpx.AsyncClient) -> list[dict[str, str]]:
    principal_response = await _request(
        client,
        "PROPFIND",
        ICLOUD_CALDAV_URL,
        depth="0",
        body=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
            "</d:prop></d:propfind>"
        ),
    )
    principal_root = ElementTree.fromstring(principal_response.content)  # noqa: S314 - XML CalDAV iCloud authentifié
    principal_href = _first_text(
        principal_root, f".//{{{DAV}}}current-user-principal/{{{DAV}}}href"
    )
    if not principal_href:
        raise ValueError("iCloud n’a pas retourné de principal CalDAV")
    principal_url = _icloud_url(str(principal_response.url), principal_href)

    home_response = await _request(
        client,
        "PROPFIND",
        principal_url,
        depth="0",
        body=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
        ),
    )
    home_root = ElementTree.fromstring(home_response.content)  # noqa: S314 - XML CalDAV iCloud authentifié
    home_href = _first_text(home_root, f".//{{{CALDAV}}}calendar-home-set/{{{DAV}}}href")
    if not home_href:
        raise ValueError("iCloud n’a pas retourné le dossier des calendriers")
    home_url = _icloud_url(str(home_response.url), home_href)

    calendars_response = await _request(
        client,
        "PROPFIND",
        home_url,
        depth="1",
        body=(
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            "<d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>"
        ),
    )
    root = ElementTree.fromstring(calendars_response.content)  # noqa: S314 - XML CalDAV iCloud authentifié
    calendars: list[dict[str, str]] = []
    for response in root.findall(f".//{{{DAV}}}response"):
        if response.find(f".//{{{DAV}}}resourcetype/{{{CALDAV}}}calendar") is None:
            continue
        href = _first_text(response, f"./{{{DAV}}}href")
        if not href:
            continue
        url = _icloud_url(str(calendars_response.url), href)
        name = _first_text(response, f".//{{{DAV}}}displayname") or (
            href.rstrip("/").rsplit("/", 1)[-1]
        )
        calendars.append({"name": name, "url": url})
    return calendars


def _parse_bound(value: str, *, end: bool = False) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed_date = date.fromisoformat(normalized)
        parsed = datetime.combine(parsed_date, time.max if end else time.min)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC)


def _event_bounds(
    start: str | None,
    end: str | None,
    days_ahead: int,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    if (start is None) != (end is None):
        raise ValueError("start et end doivent être fournis ensemble")
    if start is not None and end is not None:
        return _parse_bound(start), _parse_bound(end, end=True)
    local_now = now or datetime.now().astimezone()
    local_zone = local_now.tzinfo
    today = local_now.date()
    start_at = datetime.combine(today, time.min, tzinfo=local_zone)
    # `days_ahead=7` means today plus the seven following calendar days;
    # CalDAV's upper bound is exclusive, hence the extra day.
    end_at = datetime.combine(
        today + timedelta(days=days_ahead + 1),
        time.min,
        tzinfo=local_zone,
    )
    return start_at.astimezone(UTC), end_at.astimezone(UTC)


def _caldav_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _unfold_icalendar(content: str) -> list[str]:
    result: list[str] = []
    for line in content.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def _ical_text(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _event_value(name: str, value: str) -> str:
    if name in {"SUMMARY", "LOCATION", "DESCRIPTION"}:
        return _ical_text(value)
    if name in {"DTSTART", "DTEND"} and value.endswith("Z"):
        try:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).isoformat()
        except ValueError:
            return value
    if name in {"DTSTART", "DTEND"} and len(value) == 8 and value.isdigit():
        try:
            return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}").isoformat()
        except ValueError:
            return value
    return value


def _parse_events(content: str, calendar: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    wanted = {"UID", "SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION", "STATUS", "URL"}
    for line in _unfold_icalendar(content):
        if line == "BEGIN:VEVENT":
            current = {"calendar": calendar}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        raw_name, value = line.split(":", 1)
        name = raw_name.split(";", 1)[0].upper()
        if name in wanted and name.casefold() not in current:
            current[name.casefold()] = _event_value(name, value)
            if ";TZID=" in raw_name.upper() and name in {"DTSTART", "DTEND"}:
                current[f"{name.casefold()}_timezone"] = (
                    raw_name.split("TZID=", 1)[1].split(";", 1)[0]
                )
    return events


async def icloud_list_calendars(
    ctx: RunContext[Any],
    justification: str = "",
) -> dict[str, Any]:
    """List calendars from the configured iCloud CalDAV account."""
    try:
        username, password = _credentials(ctx)
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password), timeout=30, follow_redirects=True
        ) as client:
            calendars = await _discover_calendars(client)
        return _success(
            {"calendars": [{"name": item["name"]} for item in calendars]},
            count=len(calendars),
        )
    except (ValueError, PermissionError, httpx.HTTPError, ElementTree.ParseError, OSError) as exc:
        return _failure("caldav", str(exc))


async def icloud_list_events(  # noqa: C901 - dette: parcours CalDAV multi-étapes
    ctx: RunContext[Any],
    start: str | None = None,
    end: str | None = None,
    days_ahead: Annotated[int, Field(ge=0, le=365)] = 7,
    calendar: str | None = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    justification: str = "",
) -> dict[str, Any]:
    """Read iCloud events in an ISO-8601 interval of at most 366 days."""
    try:
        start_at, end_at = _event_bounds(start, end, days_ahead)
        if end_at <= start_at:
            raise ValueError("la fin doit être postérieure au début")
        if (end_at - start_at).days > 366:
            raise ValueError("la période demandée ne peut pas dépasser 366 jours")
        username, password = _credentials(ctx)
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password), timeout=30, follow_redirects=True
        ) as client:
            calendars = await _discover_calendars(client)
            if calendar:
                calendars = [
                    item
                    for item in calendars
                    if item["name"].casefold() == calendar.casefold()
                ]
                if not calendars:
                    raise ValueError(f"calendrier introuvable : {calendar}")
            events: list[dict[str, str]] = []
            for item in calendars:
                response = await _request(
                    client,
                    "REPORT",
                    item["url"],
                    depth="1",
                    body=(
                        '<?xml version="1.0" encoding="utf-8"?>'
                        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                        "<d:prop><d:getetag/><c:calendar-data><c:expand "
                        f'start="{_caldav_timestamp(start_at)}" end="{_caldav_timestamp(end_at)}"/>'
                        '</c:calendar-data></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
                        '<c:comp-filter name="VEVENT"><c:time-range '
                        f'start="{_caldav_timestamp(start_at)}" end="{_caldav_timestamp(end_at)}"/>'
                        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
                    ),
                )
                root = ElementTree.fromstring(response.content)  # noqa: S314 - XML CalDAV iCloud authentifié
                for node in root.findall(f".//{{{CALDAV}}}calendar-data"):
                    if node.text:
                        events.extend(_parse_events(node.text, item["name"]))
                        if len(events) >= limit:
                            break
                if len(events) >= limit:
                    break
        events = events[:limit]
        events.sort(key=lambda item: item.get("dtstart", ""))
        return _success(
            {"start": start_at.isoformat(), "end": end_at.isoformat(), "events": events},
            count=len(events),
            truncated=len(events) == limit,
        )
    except (ValueError, PermissionError, httpx.HTTPError, ElementTree.ParseError, OSError) as exc:
        return _failure("caldav", str(exc))


class ICloudCalendarModule:
    def toolsets(self):
        return [FunctionToolset(tools=[icloud_list_calendars, icloud_list_events])]

    def instructions(self):
        return [
            "Use icloud_list_calendars and icloud_list_events for read-only "
            "iCloud calendar access. "
            "Never ask for credential values in chat; request the secret names "
            f"{USERNAME_SECRET} and {PASSWORD_SECRET} when configuration is missing."
        ]

    def capabilities(self):
        return []


module = ICloudCalendarModule()
