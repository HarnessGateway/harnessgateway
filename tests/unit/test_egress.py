import pytest

from harnessgateway.domain.egress import Entry, Policy, entries


@pytest.mark.parametrize("text", ["example.com", "*.example.com", "*", "api.example.com:8443", "!*.evil.com",
                                  "!x.example.com:80"])
def test_entries_parse_and_print_back(text):
    assert str(Entry.parse(text)) == text


@pytest.mark.parametrize("text", ["", "com", "*.com", "http://x.com", "x.com:0", "x.com:70000", "a..b", "**.x.com",
                                  "x.com/path", 5, "!!x.com"])
def test_bad_entries_are_refused(text):
    with pytest.raises(ValueError):
        Entry.parse(text)


def test_wildcards_match_subdomains_at_any_depth_never_the_domain_or_a_lookalike():
    e = Entry.parse("*.example.com")
    assert e.matches("a.example.com", 443) and e.matches("a.b.example.com", 443)
    assert not e.matches("example.com", 443) and not e.matches("badexample.com", 443)
    assert not e.matches("a.example.com", 80)


def test_a_deny_at_either_level_wins_and_a_harness_widens_the_org():
    org, harness = ["*.npmjs.org", "github.com"], ["pypi.org", "!github.com"]
    p = Policy.of(org, harness)
    assert p.permits("registry.npmjs.org", 443) and p.permits("pypi.org", 443)
    assert not p.permits("github.com", 443) and not p.permits("example.com", 443)
    assert Policy.of(["*", "!*.internal.example.com"]).permits("example.com", 443)
    assert not Policy.of(["*", "!*.internal.example.com"]).permits("a.internal.example.com", 443)


def test_entries_are_normalized_and_deduplicated():
    assert entries(["Example.COM", "example.com:443", "!X.org"]) == ["!x.org", "example.com"]
