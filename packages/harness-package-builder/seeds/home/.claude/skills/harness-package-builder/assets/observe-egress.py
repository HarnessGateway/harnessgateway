#!/usr/bin/env python3
"""Run a command and print every host it asked the network for: a CONNECT-logging proxy on localhost, chained to the
sandbox's door (the HTTPS_PROXY of this environment), with the command's proxy variables pointed at it. The hosts it
prints are what a package's `install.egress` (for the install) and `run.egress` (for the CLI) must declare.

    python3 observe-egress.py -- npm install -g --prefix /tmp/tools some-cli@1.2.3
    python3 observe-egress.py -- some-cli -p "write hello.py"

Stdlib only. Plain-HTTP proxy requests (GET http://host/...) are logged by host too; everything is tunnelled as it is.
"""
import contextlib
import os
import socket
import subprocess
import sys
import threading
from urllib.parse import urlsplit

seen: dict[str, int] = {}
lock = threading.Lock()
upstream = urlsplit(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "http://hg-door:3128")


def splice(a: socket.socket, b: socket.socket) -> None:
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)


def handle(client: socket.socket) -> None:
    try:
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, target = line.split(" ")[:2]
        host = target.rsplit(":", 1)[0] if method == "CONNECT" else (urlsplit(target).hostname or "?")
        port = int(target.rsplit(":", 1)[1]) if method == "CONNECT" and ":" in target else (urlsplit(target).port or 80)
        with lock:
            seen[f"{host}:{port}"] = seen.get(f"{host}:{port}", 0) + 1
        up = socket.create_connection((upstream.hostname or "hg-door", upstream.port or 3128), timeout=30)
        up.sendall(head)                                   # the door answers the CONNECT (or relays the request) itself
        threading.Thread(target=splice, args=(client, up), daemon=True).start()
        splice(up, client)
    except OSError:
        pass
    finally:
        client.close()


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(64)
    port = server.getsockname()[1]

    def serve() -> None:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()
    proxy = f"http://127.0.0.1:{port}"
    env = {**os.environ, "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy}
    code = subprocess.call(argv, env=env)
    print("\n--- hosts asked for (egress entries) ---", file=sys.stderr)
    for key in sorted(seen):
        host, port_s = key.rsplit(":", 1)
        print(host if port_s == "443" else key, f"({seen[key]}x)", file=sys.stderr)
    if not seen:
        print("(none)", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
