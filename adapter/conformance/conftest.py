"""Conformance kit: checks a running adapter against the device-job contract (openapi.json).

    uv run pytest conformance --adapter http://lab-host:8000    # any adapter
    uv run pytest conformance                                   # this repo's: each binding and protocol in OWN

Checks run in parallel (pytest-xdist, `-n auto` in pyproject.toml). Against real hardware, add
`-n0` so one check at a time submits jobs to it.

The kit calls only the contract's operations and validates every reply against the spec.
Loading a plate is an operator action, outside the contract, so the adapter under test must
load queued plates itself: here, the simulated operator (NF_LAB_SIMULATED_OPERATOR=1).
"""

import json
import os
import secrets
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
    g.addoption("--capability", default="liquid-handling", help="with --adapter: a capability the adapter offers")
    g.addoption("--protocol", default="plate_transfer", help="with --adapter: a protocol that capability runs and that succeeds")
    g.addoption("--job-timeout", type=float, default=120, help="seconds a job may take to finish")
    g.addoption("--token", default=os.environ.get("NF_LAB_TOKEN"),
                help="the adapter's bearer token (default: $NF_LAB_TOKEN); unset: the auth check is skipped")


def pytest_report_header(config):
    if url := config.getoption("adapter"):
        return [f"WARNING: the kit submits real jobs to {url} and leaves them in its ledger. "
                "Run it against a simulation adapter only, never one driving real robots."]


# This repo's adapters, checked when no --adapter is given: backend → its robots, and the
# (backend, capability, protocol) runs of the kit. One adapter per backend serves all its runs.
OWN_DEVICES = {"fake": "2", "plr": "liquid-handling:2,plate-reading:2,thermocycling:2,qpcr:2,"
                                   "shaking:2,heating-shaking:2,temperature-control:2,tilting:2,"
                                   "centrifugation:2,incubation:2,pumping:2,weighing:2,powder-dispensing:2"}
OWN = [("fake", "liquid-handling", "plate_transfer"),
       ("plr", "liquid-handling", "plate_transfer"),
       ("plr", "plate-reading", "plate_read"),
       ("plr", "thermocycling", "pcr"),
       ("plr", "qpcr", "qpcr"),
       ("plr", "shaking", "plate_mix"),
       ("plr", "heating-shaking", "plate_lysis"),
       ("plr", "temperature-control", "heat_inactivation"),
       ("plr", "tilting", "plate_tilt"),
       ("plr", "centrifugation", "plate_spin"),
       ("plr", "incubation", "plate_incubate"),
       ("plr", "pumping", "reagent_dispense"),
       ("plr", "weighing", "plate_weigh"),
       ("plr", "powder-dispensing", "powder_dispense")]


def pytest_generate_tests(metafunc):
    if "adapter" in metafunc.fixturenames:
        get = metafunc.config.getoption
        runs = [(get("adapter"), get("capability"), get("protocol"))] if get("adapter") else OWN
        metafunc.parametrize("adapter", runs, ids=["-".join(r[::2]) for r in runs], indirect=True, scope="module")


@pytest.fixture(scope="session")
def own_adapters(tmp_path_factory):
    """Starts this repo's adapter for a backend on first use: (base URL, token)."""
    started: dict[str, tuple[str, str]] = {}
    procs = []

    def get(backend: str) -> tuple[str, str]:
        if backend in started:
            return started[backend]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        run_dir = tmp_path_factory.mktemp(backend)
        token = secrets.token_urlsafe(16)
        env = {**os.environ, "NF_LAB_TOKEN": token, "NF_LAB_BACKEND": backend, "NF_LAB_DEVICES": OWN_DEVICES[backend],
               "NF_LAB_STEP": "0.01", "NF_LAB_SIMULATED_OPERATOR": "1", "NF_LAB_LEDGER": str(run_dir / "ledger.sqlite")}
        env.pop("NF_LAB_VISUALIZER", None)
        log = (run_dir / "adapter.log").open("w")
        proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "nf_lab_adapter.app:main", "--factory",
                                 "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=log)
        procs.append((proc, log))
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60  # PyLabRobot is slow to import
        while True:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{base}/jobs?session=up",
                                                              headers={"Authorization": f"Bearer {token}"}), timeout=1)
                break
            except OSError:
                assert proc.poll() is None and time.monotonic() < deadline, \
                    f"{backend} adapter didn't start:\n{(run_dir / 'adapter.log').read_text()}"
                time.sleep(0.2)
        started[backend] = base, token
        return started[backend]

    yield get
    for proc, log in procs:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()


@pytest.fixture(scope="module")
def adapter(request, own_adapters):
    """(base URL, token, capability, protocol) of the adapter under test: the one given, or one of this repo's."""
    target, capability, protocol = request.param
    if target.startswith("http"):
        base = target.rstrip("/")
        try:
            urllib.request.urlopen(f"{base}/jobs?session=up", timeout=5)
        except urllib.error.HTTPError:
            pass  # it answered (a 401 without a token is fine)
        except OSError as e:
            pytest.exit(f"adapter not reachable at {base}: {getattr(e, 'reason', e)}. Is it started (and ready)?", returncode=1)
        return base, request.config.getoption("token"), capability, protocol
    return *own_adapters(target), capability, protocol


@dataclass
class Reply:
    status: int
    body: dict | list | None
    headers: dict  # lower-case names


class Contract:
    """Calls the adapter by operationId; fails on a status the spec doesn't list for that
    operation or a body that doesn't match its schema."""

    def __init__(self, base: str, spec: dict, token: str | None):
        self.base, self.spec, self.token = base, spec, token
        self.ops = {o["operationId"]: (m.upper(), p, o) for p, item in spec["paths"].items() for m, o in item.items()}

    def __call__(self, op: str, body: dict | None = None, query: dict | None = None, token: str | None = "",
                 **params: str) -> Reply:
        """token: "" sends the kit's own token, None none, anything else that."""
        method, path, o = self.ops[op]
        url = self.base + path.format(**{k: urllib.parse.quote(v, safe="") for k, v in params.items()})
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if data else {}
        sent = self.token if token == "" else token
        if sent:
            headers["Authorization"] = f"Bearer {sent}"
        req = urllib.request.Request(url, data, headers, method=method)
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
def api(adapter, request) -> Contract:
    base, token, _, _ = adapter
    return Contract(base, json.loads(Path(request.config.getoption("spec")).read_text()), token)


@pytest.fixture
def opts(adapter, request) -> dict:
    return {"capability": adapter[2], "protocol": adapter[3], "job_timeout": request.config.getoption("job_timeout")}
