"""Lists the OpenAI way: {"object": "list", "data", "first_id", "last_id", "has_more"}, and the clause that confines a
run-only key's reads to its own sessions."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.access import Principal
from ..errors import Invalid
from ..limits import PAGE_MAX


def own(p: Principal) -> tuple[str, list[str]]:
    """A clause confining a query on sessions `s` to the principal's own, when that is all it may read."""
    return (" AND s.created_by = %s", [p.actor]) if p.owns_only else ("", [])


def page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    data = rows[:limit]
    return {"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None, "has_more": len(rows) > limit}


def check_limit(limit: int) -> int:
    if not 1 <= limit <= PAGE_MAX:
        raise Invalid(f"limit is 1 to {PAGE_MAX}", param="limit")
    return limit


def ts(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value else None
