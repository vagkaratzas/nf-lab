import json
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.backends import FakeBackend

FINAL = {"done", "failed_preflight", "failed_in_protocol", "stopped"}


def adapter(ledger: Path, step: float = 0.05, silent_after: float = 600) -> TestClient:
    return TestClient(create_app(str(ledger), [Device("fake-1", "liquid-handling", FakeBackend(step), silent_after)]))


def req(key: str, protocol: str = "ok", **kw) -> dict:
    return {"capability": "liquid-handling", "protocol": protocol, "protocol_version": "1.0",
            "sample_ids": ["S1", "S2"], "idempotency_key": key, **kw}


def wait(c: TestClient, job_id: str, until=lambda j: j["state"] in FINAL, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = c.get(f"/jobs/{job_id}").json()
        if until(job):
            return job
        assert time.monotonic() < deadline, f"timed out waiting on {job}"
        time.sleep(0.01)


def run(c: TestClient, key: str, protocol: str = "ok") -> dict:
    """Submit, wait for a final state, release the device."""
    job = wait(c, c.post("/jobs", json=req(key, protocol)).json()["job_id"])
    c.post(f"/jobs/{job['job_id']}/release")
    return job


def test_same_key_returns_same_job(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        first, again = c.post("/jobs", json=req("k")), c.post("/jobs", json=req("k"))
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json()["job_id"] == again.json()["job_id"]

        done = wait(c, first.json()["job_id"])
        assert done["state"] == "done" and done["result"]["wells"] == 2
        after = c.post("/jobs", json=req("k"))
        assert after.status_code == 200 and after.json()["job_id"] == done["job_id"]

        assert c.post("/jobs", json=req("k", sample_ids=["S9"])).status_code == 409


def test_two_jobs_on_one_device_run_serially(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        a = c.post("/jobs", json=req("a")).json()
        b = c.post("/jobs", json=req("b")).json()
        a = wait(c, a["job_id"])
        time.sleep(0.2)
        assert c.get(f"/jobs/{b['job_id']}").json()["state"] == "queued"  # locked until release

        c.post(f"/jobs/{a['job_id']}/release")
        b = wait(c, b["job_id"])
        assert datetime.fromisoformat(b["started_at"]) > datetime.fromisoformat(a["finished_at"])


def test_ledger_survives_restart(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    with adapter(ledger) as c:
        done = run(c, "a")
        silent = c.post("/jobs", json=req("b", "silent")).json()
        wait(c, silent["job_id"], lambda j: not j["stoppable"])  # material touched, then the adapter dies

    with adapter(ledger) as c:
        again = c.post("/jobs", json=req("a"))
        assert again.status_code == 200 and again.json()["job_id"] == done["job_id"]

        b = c.get("/lookup/b").json()
        assert (b["state"], b["reason"]) == ("failed_in_protocol", "adapter_restarted")
        assert c.post("/jobs", json=req("b", "silent")).status_code == 200  # never re-run


def test_only_preflight_failures_get_a_new_attempt(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        first = run(c, "p", "preflight_fail")
        assert first["state"] == "failed_preflight" and first["stoppable"]
        retry = c.post("/jobs", json=req("p", "preflight_fail"))
        assert retry.status_code == 201 and retry.json()["attempt"] == 2
        wait(c, retry.json()["job_id"])
        c.post(f"/jobs/{retry.json()['job_id']}/release")

        for key, protocol, state in [("x", "protocol_fail", "failed_in_protocol"), ("e", "estop", "stopped")]:
            job = run(c, key, protocol)
            assert job["state"] == state and not job["stoppable"]
            repeat = c.post("/jobs", json=req(key, protocol))
            assert repeat.status_code == 200 and repeat.json()["job_id"] == job["job_id"]


def test_preflight_failure_frees_the_device_without_release(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        first = wait(c, c.post("/jobs", json=req("p", "preflight_fail")).json()["job_id"])  # never released
        assert first["state"] == "failed_preflight" and first["released_at"] is None
        retry = c.post("/jobs", json=req("p", "preflight_fail")).json()  # an exit-75 retry: same key
        assert wait(c, retry["job_id"])["attempt"] == 2  # ran: not queued behind attempt 1
        assert wait(c, c.post("/jobs", json=req("next")).json()["job_id"])["state"] == "done"


def test_safe_stop_cancels_before_boundary_and_finishes_after(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", step=0.3) as c:
        early = c.post("/jobs", json=req("s")).json()
        wait(c, early["job_id"], lambda j: j["state"] == "preflight")
        c.post(f"/jobs/{early['job_id']}/safe_stop")
        early = wait(c, early["job_id"])
        assert (early["state"], early["reason"]) == ("failed_preflight", "safe_stop")
        c.post(f"/jobs/{early['job_id']}/release")

        late = c.post("/jobs", json=req("t")).json()
        wait(c, late["job_id"], lambda j: not j["stoppable"])
        assert c.post(f"/jobs/{late['job_id']}/safe_stop").json()["stop_requested"]
        assert c.post(f"/jobs/{late['job_id']}/release").status_code == 409  # still running
        assert wait(c, late["job_id"])["state"] == "done"


def test_silent_device_fails_and_frees_the_worker(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", silent_after=0.3) as c:
        silent = c.post("/jobs", json=req("quiet", "silent")).json()
        nxt = c.post("/jobs", json=req("next")).json()
        silent = wait(c, silent["job_id"])
        assert (silent["state"], silent["reason"]) == ("failed_in_protocol", "device_silent")  # past the boundary

        time.sleep(0.2)
        assert c.get(f"/jobs/{nxt['job_id']}").json()["state"] == "queued"  # lock held for a human
        c.post(f"/jobs/{silent['job_id']}/release")
        assert wait(c, nxt["job_id"])["state"] == "done"  # the worker wasn't stuck on the silent job


def test_committed_openapi_matches_the_app():
    committed = json.loads((Path(__file__).parents[1] / "openapi.json").read_text())
    assert committed == json.loads(json.dumps(create_app(":memory:", []).openapi())), \
        "regenerate: uv run python -m nf_lab_adapter.app > openapi.json"
