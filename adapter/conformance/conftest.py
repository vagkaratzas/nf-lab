"""Conformance kit: checks a running adapter against the device-job contract (openapi.json).

    uv run pytest conformance --adapter http://lab-host:8000    # any adapter
    uv run pytest conformance                                   # this repo's, FakeBackend and PlrSimBackend

The kit calls only the contract's operations and validates every reply against the spec.
Loading a plate is an operator action, outside the contract, so the adapter under test must
load queued plates itself: here, the simulated operator (NF_LAB_SIMULATED_OPERATOR=1).
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SPEC = Path(__file__).parents[1] / "openapi.json"


def pytest_addoption(parser):
    g = parser.getgroup("conformance")
    g.addoption("--adapter", help="URL of the adapter to check; unset: start this repo's fake and plr adapters")
    g.addoption("--spec", default=str(SPEC), help="the contract (default: adapter/openapi.json)")
    g.addoption("--capability", default="liquid-handling", help="a capability the adapter offers")
    g.addoption("--protocol", default="plate_transfer", help="a protocol that capability runs and that succeeds")
    g.addoption("--job-timeout", type=float, default=120, help="seconds a job may take to finish")


def pytest_generate_tests(metafunc):
    if "url" in metafunc.fixturenames:
        given = metafunc.config.getoption("adapter")
        metafunc.parametrize("url", [given] if given else ["fake", "plr"], indirect=True, scope="module")


@pytest.fixture(scope="module")
def url(request, tmp_path_factory):
    if request.param.startswith("http"):
        yield request.param.rstrip("/")
        return
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    run_dir = tmp_path_factory.mktemp(request.param)
    env = {**os.environ, "NF_LAB_BACKEND": request.param, "NF_LAB_DEVICES": "2", "NF_LAB_STEP": "0.05",
           "NF_LAB_SIMULATED_OPERATOR": "1", "NF_LAB_LEDGER": str(run_dir / "ledger.sqlite")}
    env.pop("NF_LAB_VISUALIZER", None)
    log = (run_dir / "adapter.log").open("w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "nf_lab_adapter.app:main", "--factory",
                             "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60  # PyLabRobot is slow to import
    while True:
        try:
            urllib.request.urlopen(f"{base}/jobs?session=up", timeout=1)
            break
        except OSError:
            assert proc.poll() is None and time.monotonic() < deadline, \
                f"{request.param} adapter didn't start:\n{(run_dir / 'adapter.log').read_text()}"
            time.sleep(0.2)
    yield base
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


@dataclass
class Reply:
    status: int
    body: dict | list | None
    headers: dict  # lower-case names


class Contract:
    """Calls the adapter by operationId; fails on a status the spec doesn't list for that
    operation or a body that doesn't match its schema."""

    def __init__(self, base: str, spec: dict):
        self.base, self.spec = base, spec
        self.ops = {o["operationId"]: (m.upper(), p, o) for p, item in spec["paths"].items() for m, o in item.items()}
        self.called: set[str] = set()

    def __call__(self, op: str, body: dict | None = None, query: dict | None = None, **params: str) -> Reply:
        method, path, o = self.ops[op]
        self.called.add(op)
        url = self.base + path.format(**{k: urllib.parse.quote(v, safe="") for k, v in params.items()})
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, raw, headers = r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}
        doc = json.loads(raw) if raw else None
        resp = o["responses"].get(str(status))
        assert resp is not None, f"{op}: status {status} isn't in the spec ({', '.join(o['responses'])}): {doc}"
        schema = resp.get("content", {}).get("application/json", {}).get("schema")
        if schema:
            Draft202012Validator({**schema, "components": self.spec["components"]}).validate(doc)
        return Reply(status, doc, headers)


@pytest.fixture(scope="module")
def api(url, request) -> Contract:
    return Contract(url, json.loads(Path(request.config.getoption("spec")).read_text()))


@pytest.fixture
def opts(request) -> dict:
    get = request.config.getoption
    return {"capability": get("capability"), "protocol": get("protocol"), "job_timeout": get("job_timeout")}
