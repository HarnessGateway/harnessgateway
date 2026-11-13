"""What the platform reads out of a Responses request body (spec §13.3, §13.8)."""

from typing import Any


def file_ids(body: dict[str, Any]) -> list[str]:
    """The ids of the uploaded files a request's input refers to, each once, in the order they appear."""
    found: dict[str, None] = {}
    given = body.get("input")
    for item in given if isinstance(given, list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") in ("input_file", "input_image") \
                    and isinstance(part.get("file_id"), str):
                found[part["file_id"]] = None
    return list(found)
