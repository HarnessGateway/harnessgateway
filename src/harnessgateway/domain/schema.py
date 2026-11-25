"""The Responses API as JSON Schemas, generated from the OpenAI SDK this platform pins: a request, the response a
caller receives, and the events of its stream. They are OpenAI's own, with nothing added and nothing removed.

The files are kept in the package that builds packages (`shipped.py` says where), so that a change of the pin shows
as a change of them, and the builder that ships with a version has that version's schemas. `harnessgateway schema`
writes them; a test fails when they are not what the pin gives. The server never reads them: it checks requests and
printed lines against the SDK's types themselves.
"""

import json
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Any

from openai.types.responses import Response, ResponseCreateParams, ResponseStreamEvent
from pydantic import TypeAdapter

SDK = f"openai {version('openai')}"            # the pin: what "the Responses API" means to this platform


def request() -> dict[str, Any]:
    """A request, as OpenAI's SDK describes one."""
    return TypeAdapter(ResponseCreateParams).json_schema()


def response() -> dict[str, Any]:
    """The response a caller receives, as OpenAI's SDK describes it."""
    return Response.model_json_schema()


def stream() -> dict[str, Any]:
    """An event of a response's stream, as OpenAI's SDK describes them."""
    return TypeAdapter(ResponseStreamEvent).json_schema()


# Each schema file: what it is, and what makes it.
FILES: dict[str, tuple[str, Callable[[], dict[str, Any]]]] = {
    "request.schema.json": ("A request to POST /v1/responses, as OpenAI's SDK describes it: unchanged.", request),
    "response.schema.json": ("The response a caller receives, as OpenAI's SDK describes it: unchanged.", response),
    "stream.schema.json": ("An event of a response's stream, as OpenAI's SDK describes them: unchanged.", stream),
}


def files() -> dict[str, str]:
    """Every schema file, as it is kept: named for the pin it came from, and written one way, so that two
    generations from one pin are the same bytes."""
    return {name: json.dumps({"$comment": f"{what} Generated from {SDK} by `harnessgateway schema`.", **make()},
                             indent=1, sort_keys=True) + "\n" for name, (what, make) in FILES.items()}


def write(folder: Path) -> list[str]:
    """Write the schema files into a folder. The names of those that changed."""
    changed = []
    for name, text in files().items():
        path = folder / name
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
            changed.append(name)
    return changed
