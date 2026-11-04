"""The database boundary: what every parameter becomes on its way to Postgres."""
from psycopg.types.json import Jsonb

from harnessgateway.db import _params


def test_params_hold_no_nul() -> None:
    """Postgres text and jsonb refuse NUL; a package's output may carry one, so U+FFFD stands in, at any depth."""
    text, data, items, number = _params(("a\0b", {"k": ["x\0", {"n": "\0"}]}, ["y\0"], 7))
    assert text == "a�b"
    assert isinstance(data, Jsonb) and data.obj == {"k": ["x�", {"n": "�"}]}
    assert items == ["y�"]
    assert number == 7
