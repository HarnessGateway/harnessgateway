"""The console's routes: its files and nothing else, an edition's files with them, and the API left alone."""

import base64
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from harnessgateway.api import console
from harnessgateway.settings import Settings, SettingsError


def client(*added: Path) -> TestClient:
    return TestClient(Starlette(routes=console.routes(added)), follow_redirects=False)


def test_the_console_is_served_at_its_path_and_the_root_leads_to_it():
    http = client()
    page = http.get("/console/")
    assert page.status_code == 200 and "<title>HarnessGateway</title>" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"] and page.headers["x-content-type-options"] == "nosniff"
    assert http.get("/console/app.js").headers["content-type"].startswith("text/javascript")
    assert [http.get(path).headers["location"] for path in ("/", "/console")] == ["/console/", "/console/"]


def test_every_file_a_page_names_is_one_the_console_has():
    names = {file.name for file in console.FILES.iterdir()}
    for file in console.FILES.glob("*.js"):
        for line in file.read_text().splitlines():
            if line.startswith("import ") and " from " in line:
                assert line.split(' from "./')[1].rstrip('";') in names, f"{file.name}: {line}"
    assert {"app.css", "app.js"} <= names and 'href="app.css"' in (console.FILES / "index.html").read_text()


def test_nothing_outside_the_console_is_served_and_nothing_is_written():
    http = client()
    for path in ("/console/nope.js", "/console/../cli.py", "/console/%2e%2e/cli.py", "/console/..%2f..%2fcli.py", "/v1/platform"):
        assert http.get(path).status_code == 404, path
    assert http.post("/console/").status_code == 405


def test_an_edition_adds_files_and_the_last_named_is_looked_in_first(tmp_path: Path):
    first, last = tmp_path / "first", tmp_path / "last"
    for folder, pages in ((first, "one"), (last, "two")):
        folder.mkdir()
        (folder / "edition.js").write_text(f"export const pages = [/* {pages} */];")
    (first / "extra.js").write_text("export {};")
    http = client(first, last)
    assert "two" in http.get("/console/edition.js").text                     # in place of the core's, and of the first's
    assert http.get("/console/extra.js").status_code == 200                  # added
    assert "import { pages }" in http.get("/console/app.js").text            # the core's own, still
    assert "pages = []" in client().get("/console/edition.js").text          # the core adds no page


def test_an_installation_says_whether_it_serves_the_console():
    env = {"HG_DATABASE_URL": "postgresql://db/hg", "HG_SECRET_KEY": base64.b64encode(bytes(32)).decode(), "HG_PROXY_KEY": "k"}
    assert Settings.from_env(env).console and not Settings.from_env({**env, "HG_CONSOLE": "off"}).console
    with pytest.raises(SettingsError, match="HG_CONSOLE is on or off"):
        Settings.from_env({**env, "HG_CONSOLE": "maybe"})
