"""The console's routes: its files and nothing else, an edition's files with them, and the API left alone."""

import base64
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from harnessgateway.api import console
from harnessgateway.settings import Settings, SettingsError


def client(*added: tuple[str, Path]) -> TestClient:
    return TestClient(Starlette(routes=console.routes(added)), follow_redirects=False)


def test_the_console_is_served_at_its_path_and_the_root_leads_to_it():
    http = client()
    page = http.get("/console/")
    assert page.status_code == 200 and "<title>HarnessGateway</title>" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"] and page.headers["x-content-type-options"] == "nosniff"
    assert http.get("/console/app.js").headers["content-type"].startswith("text/javascript")
    assert [http.get(path).headers["location"] for path in ("/", "/console")] == ["/console/", "/console/"]


def test_every_file_a_module_or_the_page_names_is_one_the_console_has():
    modules = list(console.FILES.rglob("*.js"))
    assert len(modules) > 20
    for module in modules:
        for named in re.findall(r'^import [^"]*"(\.[^"]+)";', module.read_text(), re.M):
            assert (module.parent / named).resolve().is_file(), f"{module.relative_to(console.FILES)} imports {named}"
    page = (console.FILES / "index.html").read_text()
    for named in re.findall(r'<(?:link[^>]*href|script[^>]*src)="([^":]+)"', page):
        assert (console.FILES / named).is_file(), f"index.html names {named}"


def test_nothing_outside_the_console_is_served_and_nothing_is_written():
    http = client()
    for path in ("/console/nope.js", "/console/../cli.py", "/console/%2e%2e/cli.py", "/console/..%2f..%2fcli.py", "/v1/platform"):
        assert http.get(path).status_code == 404, path
    assert http.post("/console/").status_code == 405


def test_what_renders_an_agents_document_is_served_sandboxed_and_the_console_is_not():
    http = client()
    assert http.get("/console/components/viewer/viewer.html").headers["content-security-policy"].startswith("sandbox ")
    assert "sandbox" not in http.get("/console/").headers["content-security-policy"]


def test_each_extension_adds_its_files_under_its_own_name_and_none_takes_the_place_of_another(tmp_path: Path):
    first, last = tmp_path / "first", tmp_path / "last"
    for folder, pages in ((first, "one"), (last, "two")):
        folder.mkdir()
        (folder / "edition.js").write_text(f"export const pages = [/* {pages} */];")
    (first / "app.js").write_text("// not the console's")
    (tmp_path / "secret.txt").write_text("outside both")
    http = client(("first", first), ("last", last))
    assert http.get("/console/extensions.json").json() == ["first", "last"]      # in the order they were named
    assert "one" in http.get("/console/x/first/edition.js").text and "two" in http.get("/console/x/last/edition.js").text
    assert "default-src 'self'" in http.get("/console/x/first/edition.js").headers["content-security-policy"]
    assert "import { editions }" in http.get("/console/app.js").text            # the console's own, whatever an extension holds
    assert http.get("/console/x/first/app.js").text == "// not the console's"
    for path in ("/console/edition.js", "/console/x/nobody/edition.js", "/console/x/first/../secret.txt",
                 "/console/x/first/%2e%2e/secret.txt", "/console/x/first/..%2fsecret.txt", "/console/x/last/app.js"):
        assert http.get(path).status_code == 404, path
    assert client().get("/console/extensions.json").json() == []                # the core adds nothing


def test_an_installation_says_whether_it_serves_the_console():
    env = {"HG_DATABASE_URL": "postgresql://db/hg", "HG_SECRET_KEY": base64.b64encode(bytes(32)).decode(), "HG_PROXY_KEY": "k"}
    assert Settings.from_env(env).console and not Settings.from_env({**env, "HG_CONSOLE": "off"}).console
    with pytest.raises(SettingsError, match="HG_CONSOLE is on or off"):
        Settings.from_env({**env, "HG_CONSOLE": "maybe"})
