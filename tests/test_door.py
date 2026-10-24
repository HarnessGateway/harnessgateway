"""Layer 1: the door's host rule: public addresses only, checked after resolution."""
import asyncio

import pytest

from harnessgateway.runtimes.door import Forwarder, check_host, is_public, normalize, resolve


def test_normalize():
    assert normalize("api.x.com") == "api.x.com:443"
    assert normalize("https://api.x.com/v1/") == "api.x.com:443"
    assert normalize("http://x:8080") == "x:8080"
    assert normalize("Host:9") == "host:9"


def test_public_addresses():
    assert is_public("8.8.8.8") and is_public("2606:4700::1111")
    for private in ("127.0.0.1", "10.1.2.3", "172.17.0.1", "192.168.1.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0", "224.0.0.1"):
        assert not is_public(private), private


def test_check_host():
    assert check_host("api.deepseek.com") == "api.deepseek.com" and check_host("8.8.8.8:53") == "8.8.8.8"
    assert check_host("10.0.0.1", allow_private=True) == "10.0.0.1"
    for bad in ("10.0.0.1", "169.254.169.254:80", "localhost", "x.localhost", "[::1]:443", "a b", "", "-x"):
        with pytest.raises(ValueError):
            check_host(bad)


def test_resolution_filters_private_answers():
    assert asyncio.run(resolve("localhost", 80, allow_private=False)) == []
    assert asyncio.run(resolve("localhost", 80, allow_private=True))
    assert asyncio.run(resolve("8.8.8.8", 53, allow_private=False)) == ["8.8.8.8"]


def test_forwarder_url():
    assert Forwarder("https://gw.example").port == 443 and Forwarder("http://10.0.0.5:8081").port == 8081
    with pytest.raises(ValueError):
        Forwarder("gw.example")
