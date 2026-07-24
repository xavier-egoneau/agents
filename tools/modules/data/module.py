from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field
from pydantic_ai import FunctionToolset, RunContext


def _path(ctx: RunContext[Any], raw: str) -> Path:
    candidate = Path(raw).expanduser()
    return (ctx.deps.workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _result(data: Any, **metadata: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None, "metadata": metadata}


async def json_query(
    ctx: RunContext[Any],
    path: str,
    query: str = "",
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    justification: str = "",
) -> dict[str, Any]:
    """Read JSON and traverse dot-separated object keys or numeric list indexes."""
    data = json.loads(_path(ctx, path).read_text(encoding="utf-8"))
    value = data
    for part in filter(None, query.split(".")):
        value = value[int(part)] if isinstance(value, list) else value[part]
    truncated = isinstance(value, list) and len(value) > limit
    if isinstance(value, list):
        value = value[:limit]
    return _result(value, query=query, truncated=truncated)


async def csv_query(
    ctx: RunContext[Any],
    path: str,
    columns: list[str] | None = None,
    where_column: str | None = None,
    equals: str | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    justification: str = "",
) -> dict[str, Any]:
    """Filter CSV rows by one exact value and project selected columns."""
    with _path(ctx, path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        selected = columns or fields
        unknown = set(selected) - set(fields)
        if unknown or (where_column and where_column not in fields):
            raise ValueError(f"unknown columns: {sorted(unknown | ({where_column} - set(fields) if where_column else set()))}")
        rows = []
        total = 0
        for row in reader:
            if where_column and row.get(where_column) != equals:
                continue
            total += 1
            if len(rows) < limit:
                rows.append({key: row.get(key) for key in selected})
    return _result(rows, total_matches=total, truncated=total > limit, columns=selected)


async def sql_query(
    ctx: RunContext[Any],
    path: str,
    query: str,
    parameters: list[Any] | None = None,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    justification: str = "",
) -> dict[str, Any]:
    """Execute one strictly read-only SQLite query."""
    prefix = query.lstrip().split(None, 1)[0].casefold() if query.strip() else ""
    if prefix not in {"select", "with", "explain"} or ";" in query.rstrip().rstrip(";"):
        raise ValueError("sql_query accepts one SELECT, WITH, or EXPLAIN statement")
    uri = f"file:{_path(ctx, path)}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        cursor = db.execute(query, parameters or [])
        rows = cursor.fetchmany(limit + 1)
    return _result([dict(row) for row in rows[:limit]], truncated=len(rows) > limit)


async def sql_execute(
    ctx: RunContext[Any],
    path: str,
    statement: str,
    parameters: list[Any] | None = None,
    justification: str = "",
) -> dict[str, Any]:
    """Execute one explicit SQLite mutation in a transaction."""
    if ";" in statement.rstrip().rstrip(";"):
        raise ValueError("only one SQL statement is accepted")
    prefix = statement.lstrip().split(None, 1)[0].casefold() if statement.strip() else ""
    if prefix not in {"insert", "update", "delete", "create", "alter", "drop"}:
        raise ValueError("unsupported mutation statement")
    with sqlite3.connect(_path(ctx, path), timeout=5) as db:
        cursor = db.execute(statement, parameters or [])
    return _result({"rows_affected": cursor.rowcount})


class DataModule:
    def toolsets(self):
        return [FunctionToolset(tools=[json_query, csv_query, sql_query, sql_execute])]

    def instructions(self):
        return ["Use sql_query for reads; sql_execute is a separate controlled mutation."]

    def capabilities(self):
        return []


module = DataModule()
