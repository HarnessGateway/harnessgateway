"""Where the platform runs, as the daemon sees it: on a host, every path is its own; in a container,
the paths behind its mounts are translated and the door's way out is the container's own network."""
from pathlib import Path

from harnessgateway.backends.sandboxes.docker import Host
from harnessgateway.backends.sandboxes.docker import DockerNode as Node

INSPECT = {
    "Mounts": [
        {"Type": "volume", "Name": "hg", "Source": "/var/lib/docker/volumes/hg/_data", "Destination": "/data"},
        {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
        {"Type": "bind", "Source": "/home/me/ca.pem", "Destination": "/data/worker/platform/ca.pem"},
        {"Type": "tmpfs", "Destination": "/tmp"},
    ],
    "NetworkSettings": {"Networks": {"hg_default": {"IPAddress": "172.19.0.4"}}},
}


def test_a_host_process_is_the_identity():
    host = Host()
    assert host.path("/srv/hg/data/worker/sessions/s1") == Path("/srv/hg/data/worker/sessions/s1")
    assert host.network == "bridge" and host.address == ""
    assert Node("img", "me").gateway_url == ""


def test_a_container_translates_its_mounts_deepest_first():
    host = Host.from_inspect(INSPECT)
    assert host.path("/data/worker/sessions/s1/workspace") == Path("/var/lib/docker/volumes/hg/_data/worker/sessions/s1/workspace")
    assert host.path("/data") == Path("/var/lib/docker/volumes/hg/_data")
    assert host.path("/data/worker/platform/ca.pem") == Path("/home/me/ca.pem")       # the deeper mount wins
    assert host.path("/var/run/docker.sock") == Path("/var/run/docker.sock")
    assert host.path("/usr/lib/python3/turn.sh") == Path("/usr/lib/python3/turn.sh")   # nothing behind it: unchanged, and invisible to the daemon
    assert host.network == "hg_default" and host.address == "172.19.0.4"


def test_a_container_without_mounts_or_networks_is_on_the_bridge():
    host = Host.from_inspect({})
    assert host == Host()


def test_the_gateway_is_reached_at_the_containers_own_address_only_when_it_is_in_this_process():
    host = Host.from_inspect(INSPECT)
    assert Node("img", "me", host=host, gateway_port=8081).gateway_url == "http://172.19.0.4:8081"
    assert Node("img", "me", host=host, gateway_port=0).gateway_url == ""
