"""Layer 3: records on both backends: put, get, list by prefix, find across tenants, compare-and-set,
scans by field."""
import pytest


def test_put_get_list_delete(records):
    assert records.get("thing", "acme", "a") is None
    records.put("thing", "acme", "a", {"n": 1})
    records.put("thing", "acme", "a", {"n": 2})
    records.put("thing", "acme", "b:1", {"n": 3})
    records.put("thing", "acme", "b:2", {"n": 4})
    records.put("thing", "globex", "a", {"n": 9})
    assert records.get("thing", "acme", "a") == {"n": 2}
    assert records.list("thing", "acme") == [{"n": 2}, {"n": 3}, {"n": 4}]
    assert records.list("thing", "acme", "b:") == [{"n": 3}, {"n": 4}]
    assert records.list("thing", "acme", "b_") == []     # an underscore is a character, not a wildcard
    assert records.delete("thing", "acme", "a") and not records.delete("thing", "acme", "a")
    assert records.list("thing", "globex") == [{"n": 9}]
    with pytest.raises(ValueError):
        records.get("thing", "acme", "bad key!")


def test_find_update_if_where_count(records):
    records.put("turn", "acme", "t1", {"status": "queued", "x": 1})
    records.put("turn", "acme", "t2", {"status": "queued", "x": 2})
    records.put("turn", "globex", "t3", {"status": "running", "x": 3})
    assert records.find("turn", "t3") == ("globex", {"status": "running", "x": 3}) and records.find("turn", "nope") is None
    assert [k for _, k, _ in records.where("turn", "status", "queued")] == ["t1", "t2"]
    assert records.count("turn", "acme", "status", "queued") == 2
    assert records.update_if("turn", "acme", "t1", "status", "queued", {"status": "claimed", "x": 1})
    assert not records.update_if("turn", "acme", "t1", "status", "queued", {"status": "claimed", "x": 1})   # already taken
    assert records.get("turn", "acme", "t1")["status"] == "claimed" and records.count("turn", "acme", "status", "queued") == 1
