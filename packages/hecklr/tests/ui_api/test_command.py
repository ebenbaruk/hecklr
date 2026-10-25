"""``hecklr ui`` and the serving of the built app (ADR 0016a)."""

import ipaddress
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio.to_thread
import click
import httpx2
import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

import hecklr.commands.ui
import hecklr.ui_api.build
from hecklr import exit_codes
from hecklr.cli import app as cli_app

from .conftest import Lab, make_app

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

INDEX = "<!doctype html><title>Hecklr</title><div id=root></div>"


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """A stand-in for the app ``npm run build`` writes."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(INDEX, encoding="utf-8")
    (static / "assets" / "app-1234.js").write_text("console.log(1)", encoding="utf-8")
    (static / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return static


@pytest.mark.parametrize("path", ["/", "/runs/01ARZ3NDEKTSV4RRFFQ69G5FAV", "/labels?run=x"])
async def test_app_routes_serve_the_single_page_app(lab: Lab, built: Path, path: str) -> None:
    async with _client(make_app(lab, built)) as client:
        response = await client.get(path)

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-cache"


async def test_built_files_are_served_as_they_are(lab: Lab, built: Path) -> None:
    async with _client(make_app(lab, built)) as client:
        script = await client.get("/assets/app-1234.js")
        icon = await client.get("/favicon.svg")
        api = await client.get("/api/meta")

    assert script.text == "console.log(1)"
    assert "javascript" in script.headers["content-type"]
    assert icon.text == "<svg/>"
    assert api.json()["project"] == "shop-agent"


async def test_paths_outside_the_app_get_the_index_not_the_file(lab: Lab, built: Path) -> None:
    (built.parent / "secret.txt").write_text("secret", encoding="utf-8")

    async with _client(make_app(lab, built)) as client:
        response = await client.get("/..%2Fsecret.txt")

    assert response.text == INDEX


async def test_a_static_file_at_the_app_root_is_served_with_its_type(lab: Lab, built: Path) -> None:
    (built / "robots.txt").write_text("User-agent: *", encoding="utf-8")
    async with _client(make_app(lab, built)) as client:
        response = await client.get("/robots.txt")

    assert response.text == "User-agent: *"
    assert response.headers["content-type"].startswith("text/plain")
    assert "cache-control" not in response.headers


@pytest.mark.parametrize("host", ["localhost:8484", "127.0.0.1", "[::1]:8484"])
async def test_local_host_names_are_answered(lab: Lab, host: str) -> None:
    async with _client(make_app(lab)) as client:
        response = await client.get("/api/meta", headers={"host": host})

    assert response.status_code == 200


async def test_other_host_names_are_refused_against_dns_rebinding(lab: Lab) -> None:
    async with _client(make_app(lab)) as client:
        read = await client.get("/api/runs", headers={"host": "attacker.example"})
        write = await client.post(
            "/api/labels",
            headers={"host": "attacker.example:8484"},
            json={"grade_id": "x", "passed": True, "labeler": "eve"},
        )

    assert (read.status_code, write.status_code) == (400, 400)
    assert not (lab.output_dir / "labels.jsonl").exists()


@pytest.mark.parametrize(
    ("bind", "hosts"),
    [
        ("127.0.0.1", ["localhost", "127.0.0.1", "[::1]"]),
        ("192.168.1.20", ["localhost", "127.0.0.1", "[::1]", "192.168.1.20"]),
        ("fe80::1", ["localhost", "127.0.0.1", "[::1]", "[fe80::1]"]),
        ("myhost.lan", ["localhost", "127.0.0.1", "[::1]", "myhost.lan"]),
        ("localhost", ["localhost", "127.0.0.1", "[::1]"]),
        (str(ipaddress.IPv4Address(0)), ["*"]),
        (str(ipaddress.IPv6Address(0)), ["*"]),
    ],
)
def test_allowed_hosts_follow_the_bind(bind: str, hosts: list[str]) -> None:
    assert hecklr.commands.ui.allowed_hosts(bind) == hosts


async def test_meta_names_the_project_files_and_defaults(lab: Lab) -> None:
    async with _client(make_app(lab)) as client:
        meta = (await client.get("/api/meta")).json()

    assert meta["user"] == "ada"
    assert meta["alpha"] == 0.05
    assert meta["min_effect"] == 0.03
    assert meta["labels_file"] == str(lab.output_dir / "labels.jsonl")
    assert meta["mine_dir"] == str(lab.output_dir / "mine")
    assert meta["scenarios_dir"] == str(lab.root / "scenarios" / "mined")


def _client(app: FastAPI) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1")


# --- the command ------------------------------------------------------------------------------


class Served:
    """What ``uvicorn.run`` was asked to serve."""

    app: FastAPI
    host: str
    port: int


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Served:
    record = Served()

    def fake_run(app: FastAPI, *, host: str, port: int, **_: Any) -> None:
        record.app, record.host, record.port = app, host, port

    monkeypatch.setattr("uvicorn.run", fake_run)
    return record


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    urls: list[str] = []

    class Timer:
        def __init__(self, _: float, function: Any, args: tuple[str]) -> None:
            self.url = args[0]

        def start(self) -> None:
            urls.append(self.url)

    monkeypatch.setattr("threading.Timer", Timer)
    return urls


def _invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli_app, ["ui", *args])
    return result.exit_code, click.unstyle(result.output)


async def test_hecklr_ui_serves_the_built_app_and_opens_a_browser(
    lab: Lab, built: Path, served: Served, opened: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hecklr.commands.ui, "STATIC_DIR", built)

    code, output = await anyio.to_thread.run_sync(
        lambda: _invoke("--config", str(lab.config), "--port", "9123", "--user", "grace")
    )

    assert code == exit_codes.OK, output
    assert "http://127.0.0.1:9123/" in output
    assert (served.host, served.port) == ("127.0.0.1", 9123)
    assert opened == ["http://127.0.0.1:9123/"]
    async with _client(served.app) as client:
        assert (await client.get("/")).text == INDEX
        assert (await client.get("/api/meta")).json()["user"] == "grace"


async def test_hecklr_ui_dev_serves_the_api_only(
    lab: Lab, served: Served, opened: list[str]
) -> None:
    code, output = await anyio.to_thread.run_sync(
        lambda: _invoke("--config", str(lab.config), "--dev")
    )

    assert code == exit_codes.OK, output
    assert "npm run dev" in output
    assert opened == []
    async with _client(served.app) as client:
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/api/runs")).status_code == 200


def test_hecklr_ui_without_a_built_app_says_how_to_build_it(
    lab: Lab, tmp_path: Path, served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An install without the app and without a checkout's ui/ sources to build it from.
    monkeypatch.setattr(hecklr.commands.ui, "STATIC_DIR", tmp_path / "missing")
    monkeypatch.setattr(hecklr.ui_api.build, "UI_SOURCE", tmp_path / "no-checkout" / "ui")

    code, output = _invoke("--config", str(lab.config), "--no-open")

    assert code == exit_codes.NOT_IMPLEMENTED
    assert "npm --prefix ui ci && npm --prefix ui run build" in output
    assert not hasattr(served, "app")


def test_hecklr_ui_with_a_broken_config_is_a_config_error(tmp_path: Path, served: Served) -> None:
    config = tmp_path / "hecklr.yaml"
    config.write_text("version: 1\n", encoding="utf-8")

    code, _ = _invoke("--config", str(config))

    assert code == exit_codes.CONFIG_ERROR


# --- issue #106: a checkout install builds the app on the first `hecklr ui` ----------------------


@dataclass
class FakeNpm:
    """``npm`` on PATH: records each call; ``run build`` writes the app into the static dir."""

    static_dir: Path
    fails: str | None = None
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def run(self, args: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        self.calls.append((args, cwd))
        step = " ".join(args[1:])
        if step == self.fails:
            return subprocess.CompletedProcess(args, 1)
        if step == "run build":
            (self.static_dir / "assets").mkdir(parents=True)
            (self.static_dir / "index.html").write_text(INDEX, encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout's ``ui/`` sources with nothing built yet."""
    source = tmp_path / "repo" / "ui"
    source.mkdir(parents=True)
    (source / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(hecklr.ui_api.build, "UI_SOURCE", source)
    monkeypatch.setattr(hecklr.commands.ui, "STATIC_DIR", tmp_path / "static")
    return source


@pytest.fixture
def npm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNpm:
    fake = FakeNpm(tmp_path / "static")
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr("subprocess.run", fake.run)
    return fake


async def test_issue106_hecklr_ui_builds_the_app_from_a_checkout_on_first_start(
    lab: Lab, checkout: Path, npm: FakeNpm, served: Served
) -> None:
    code, output = await anyio.to_thread.run_sync(
        lambda: _invoke("--config", str(lab.config), "--no-open")
    )

    assert code == exit_codes.OK, output
    assert "Building the UI app" in output
    assert npm.calls == [(["/bin/npm", "ci"], checkout), (["/bin/npm", "run", "build"], checkout)]
    async with _client(served.app) as client:
        assert (await client.get("/")).text == INDEX


def test_issue106_installed_node_modules_are_reused(
    lab: Lab, checkout: Path, npm: FakeNpm, served: Served
) -> None:
    (checkout / "node_modules").mkdir()

    code, output = _invoke("--config", str(lab.config), "--no-open")

    assert code == exit_codes.OK, output
    assert npm.calls == [(["/bin/npm", "run", "build"], checkout)]


def test_issue106_a_built_app_is_not_rebuilt(
    lab: Lab, checkout: Path, npm: FakeNpm, served: Served, built: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setattr(hecklr.commands.ui, "STATIC_DIR", built)

    code, output = _invoke("--config", str(lab.config), "--no-open")

    assert code == exit_codes.OK, output
    assert npm.calls == []


def test_issue106_without_node_the_error_names_node_and_the_exact_command(
    lab: Lab, checkout: Path, served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)

    code, output = _invoke("--config", str(lab.config), "--no-open")

    assert code == exit_codes.NOT_IMPLEMENTED
    flat = " ".join(output.split())
    assert "Node.js 20.19 or newer" in flat
    assert "npm --prefix ui ci && npm --prefix ui run build" in flat
    assert not hasattr(served, "app")


def test_issue106_a_failed_npm_step_is_reported(
    lab: Lab, checkout: Path, npm: FakeNpm, served: Served
) -> None:
    npm.fails = "ci"

    code, output = _invoke("--config", str(lab.config), "--no-open")

    assert code == exit_codes.NOT_IMPLEMENTED
    assert "`npm ci`" in output
    assert "exit code 1" in output
    assert [args for args, _ in npm.calls] == [["/bin/npm", "ci"]]
    assert not hasattr(served, "app")


def test_issue106_a_build_that_writes_elsewhere_is_reported(
    lab: Lab, checkout: Path, npm: FakeNpm, served: Served, tmp_path: Path
) -> None:
    npm.static_dir = tmp_path / "elsewhere"

    code, output = _invoke("--config", str(lab.config), "--no-open")

    assert code == exit_codes.NOT_IMPLEMENTED
    assert "did not write" in output
