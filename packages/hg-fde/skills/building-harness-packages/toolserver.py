#!/usr/bin/env python3
"""A tool server for trying a package with: an MCP server over HTTP, with one tool, `secret_number`, that answers
4242. It prints the port it listens on, then serves until it is ended.

It stands for a server a request names in `tools`. Python 3.9 or later, standard library only.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOOL = {"name": "secret_number", "description": "Returns the secret number.",
        "inputSchema": {"type": "object", "properties": {}}}


def answer(call: dict[str, Any]) -> dict[str, Any] | None:
    """The answer to one JSON-RPC message; None for a notification, which has none."""
    method, given = call.get("method"), call.get("params") or {}
    if "id" not in call:
        return None
    if method == "initialize":
        result: dict[str, Any] = {"protocolVersion": given.get("protocolVersion", "2025-03-26"),
                                  "capabilities": {"tools": {}}, "serverInfo": {"name": "numbers", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call" and given.get("name") == TOOL["name"]:
        result = {"content": [{"type": "text", "text": "4242"}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": call["id"], "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": call["id"], "result": result}


class Server(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        sent = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        answers = [each for each in map(answer, sent if isinstance(sent, list) else [sent]) if each is not None]
        body = json.dumps(answers if isinstance(sent, list) else answers[0]).encode() if answers else b""
        self.send_response(200 if answers else 202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                        # it opens no stream of its own
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_DELETE = do_GET

    def log_message(self, *_: object) -> None:       # standard error is the trial's log: it says nothing there
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    print(server.server_address[1], flush=True)
    server.serve_forever()
