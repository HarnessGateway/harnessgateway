#!/usr/bin/env bash
# Run (or upgrade) the node role on this host as a container with the Docker socket, joined to the control plane named
# in node.env. The node directory is mounted at the same path inside and out: sandboxes are sibling containers that
# bind-mount their session directories from the host.
set -euo pipefail
cd "$(dirname "$0")"
[ -f node.env ] || { echo "copy node.env.example to node.env and fill it in"; exit 1; }
IMAGE=${HG_IMAGE:-harnessgateway/platform:1}
DIR=$(grep -E '^HG_NODE_DIR=' node.env | cut -d= -f2-)
DIR=${DIR:-/var/lib/harnessgateway}
mkdir -p "$DIR"
docker pull "$IMAGE" || true
docker rm -f harnessgateway-node >/dev/null 2>&1 || true
docker run -d --name harnessgateway-node --restart unless-stopped --user 0:0 --env-file node.env \
  -e HG_NODE_DIR="$DIR" -v /var/run/docker.sock:/var/run/docker.sock -v "$DIR:$DIR" \
  "$IMAGE" serve --role node
echo "node started; logs: docker logs -f harnessgateway-node"
