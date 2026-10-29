"""A turn's MCP servers: the package's catalog, narrowed or extended by the request (SPEC §3.3), with every
caller-supplied credential replaced by a placeholder (SPEC §8.3)."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .. import ids
from ..errors import Invalid
from . import egress
from .manifest import McpServer
from .names import NAME


@dataclass(frozen=True)
class Binding:
    """A secret the door injects: where the sandbox has `placeholder`, the door sends `value`, only to `hosts`."""
    placeholder: str
    value: str
    hosts: tuple[str, ...]


@dataclass(frozen=True)
class Resolved:
    servers: list[dict[str, Any]]      # /run/hg/mcp.json; catalog headers still hold ${SECRET} references
    bindings: list[Binding]            # the caller's own credentials, for this turn only
    body: dict[str, Any]               # the request with those credentials replaced by their placeholders


def _allowed(value: Any) -> list[str] | None:
    """A Responses mcp tool's `allowed_tools`: a list of names, or {"tool_names": [...]}."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict) and isinstance(value.get("tool_names"), list):
        return [str(v) for v in value["tool_names"]]
    return None


def _narrow(catalog: list[str] | None, request: list[str] | None) -> list[str] | None:
    if request is None:
        return catalog
    return [t for t in request if catalog is None or t in catalog]


def resolve(catalog: dict[str, McpServer], body: dict[str, Any], policy: egress.Policy) -> Resolved:
    body = copy.deepcopy(body)
    selected: dict[str, list[str] | None] = {}
    added: list[dict[str, Any]] = []
    bindings: list[Binding] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        label = str(tool.get("server_label") or "")
        if not NAME.fullmatch(label):
            raise Invalid(f"an mcp tool's server_label is a name ([a-z0-9][a-z0-9._-]*), not {label!r}", param="tools")
        if tool.get("connector_id"):
            raise Invalid("connectors are not supported: name a server_url, or a server the harness provides",
                          param="tools")
        url = tool.get("server_url")
        if not url:
            if label not in catalog:
                raise Invalid(f"the harness has no MCP server {label!r}; it provides: {', '.join(catalog) or 'none'}",
                              param="tools", code="mcp_server_not_found")
            selected[label] = _narrow(catalog[label].allowed_tools, _allowed(tool.get("allowed_tools")))
            continue
        if label in catalog:
            raise Invalid(f"{label!r} is the harness's own MCP server: select it without a server_url",
                          param="tools", code="mcp_label_conflict")
        u = urlsplit(str(url))
        if u.scheme != "https" or not u.hostname:
            raise Invalid(f"an mcp server_url is an https URL, not {url!r}", param="tools")
        port = u.port or 443
        if not policy.permits(u.hostname, port):
            raise Invalid(f"{u.hostname}:{port} is not in this harness's egress, so MCP server {label!r} cannot be "
                          "reached; the organization or the harness must allow it", param="tools",
                          code="mcp_server_not_allowed")
        host = egress.host_entry(u.hostname, port)
        headers: dict[str, str] = {}
        for name, value in (tool.get("headers") or {}).items():
            placeholder = ids.new_secret("placeholder")
            bindings.append(Binding(placeholder, str(value), (host,)))
            headers[str(name)] = placeholder
            tool["headers"][name] = placeholder
        if tool.get("authorization"):
            placeholder = ids.new_secret("placeholder")
            bindings.append(Binding(placeholder, str(tool["authorization"]), (host,)))
            headers["Authorization"] = f"Bearer {placeholder}"
            tool["authorization"] = placeholder
        added.append({"label": label, "url": str(url), "transport": "http", "headers": headers,
                      "allowed_tools": _allowed(tool.get("allowed_tools"))})
    active = selected if selected else {label: s.allowed_tools for label, s in catalog.items()}
    servers = [{"label": label, "url": catalog[label].url, "transport": catalog[label].transport,
                "headers": dict(catalog[label].headers), "allowed_tools": allowed} for label, allowed in active.items()]
    return Resolved(servers + added, bindings, body)
