import json
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from nf_lab_adapter.app import Device, contract_spec, create_app, device_specs
from nf_lab_adapter.backends import FakeBackend

FINAL = {"done", "failed_preflight", "failed_in_protocol", "stopped", "voided"}


def adapter(ledger: Path, step: float = 0.05, silent_after: float = 600, operator: bool = True) -> TestClient:
    """operator: the simulated operator unloads done plates and loads queued ones; off = a person does."""
    return TestClient(create_app(str(ledger), [Device("fake-1", "liquid-handling", FakeBackend(step), silent_after,
                                                      simulated_operator=operator)]))


def wells(*tokens: str) -> list[dict]:
    """wells("A1:S1", "B1:NC1:negative_control")"""
    out = []
    for t in tokens:
        pos, sample, *kind = t.split(":")
        out.append({"row": pos[0], "column": int(pos[1:]), "sample": sample, "type": kind[0] if kind else "sample"})
    return out


def req(key: str, protocol: str = "ok", **kw) -> dict:
    return {"capability": "liquid-handling", "protocol": protocol, "protocol_version": "1.0",
            "plate": "P1", "wells": wells("A1:S1", "B1:S2"), "idempotency_key": key, **kw}


def wait(c: TestClient, job_id: str, until=lambda j: j["state"] in FINAL, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = c.get(f"/jobs/{job_id}").json()
        if until(job):
            return job
        assert time.monotonic() < deadline, f"timed out waiting on {job}"
        time.sleep(0.01)


def void_and_unload(c: TestClient, job: dict) -> None:
    """A person checks the deck after a failed or stopped job, presses Failed (void), then unloads."""
    c.post(f"/jobs/{job['job_id']}/resolve", json={"outcome": "void"})
    c.post(f"/jobs/{job['job_id']}/release")


def run(c: TestClient, key: str, protocol: str = "ok") -> dict:
    """Submit, wait for a final state, release the device (a no-op once the simulated operator has;
    refused for a job failed in protocol or stopped, which a person resolves first)."""
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

        assert c.post("/jobs", json=req("k", wells=wells("B1:S2", "A1:S1"))).status_code == 200  # order-free
        assert c.post("/jobs", json=req("k", wells=wells("A1:S9"))).status_code == 409
        assert c.post("/jobs", json=req("k", plate="P2")).status_code == 409


def test_plate_map_is_validated(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        for bad in [wells("A1:S1", "A1:S2"), wells("Q1:S1"), wells("A25:S1"), wells("A1:S1:standard"), []]:
            assert c.post("/jobs", json=req("v", wells=bad)).status_code == 422, bad
        stored = c.post("/jobs", json=req("v", wells=wells("H12:BLK1:blank", "C6:S43", "A2:NC1:negative_control")))
        assert [(w["row"], w["column"]) for w in stored.json()["wells"]] == [("A", 2), ("C", 6), ("H", 12)]


def test_a_ledger_from_an_older_adapter_is_refused(tmp_path):
    import sqlite3
    from nf_lab_adapter.ledger import SCHEMA, VERSION
    sqlite3.connect(tmp_path / "old.sqlite").executescript(SCHEMA.replace(f"user_version = {VERSION}", "user_version = 1"))
    try:
        create_app(str(tmp_path / "old.sqlite"), [])
    except RuntimeError as e:
        assert f"ledger version 1, this adapter needs {VERSION}" in str(e)
    else:
        raise AssertionError("old ledger accepted")


def test_fake_plate_transfer_returns_the_plr_result_shape(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        job = run(c, "pt", "plate_transfer")
        assert job["result"] == {"plate": "P1", "wells": [
            {"row": "A", "column": 1, "sample": "S1", "type": "sample", "volume_ul": 150.0},
            {"row": "B", "column": 1, "sample": "S2", "type": "sample", "volume_ul": 150.0}]}


def test_request_log_records_contract_calls_and_operator_actions(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", operator=False) as c:
        job = c.post("/jobs", json=req("a")).json()
        c.get(f"/jobs/{job['job_id']}")
        c.get("/devices")  # the dashboard's own polls stay out
        load(c, "P1")
    log = [json.loads(line) for line in (tmp_path / "ledger.requests.jsonl").read_text().splitlines()]
    assert [(e["method"], e["path"], e["status"], e.get("location")) for e in log] == [
        ("POST", "/jobs", 201, f"/jobs/{job['job_id']}"),
        ("GET", f"/jobs/{job['job_id']}", 200, None),
        ("POST", "/devices/fake-1/load", 200, None)]


def test_two_jobs_on_one_device_run_serially(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", operator=False) as c:
        a = c.post("/jobs", json=req("a")).json()
        b = c.post("/jobs", json=req("b")).json()
        load(c, "P1")
        a = wait(c, a["job_id"])
        time.sleep(0.2)
        assert c.get(f"/jobs/{b['job_id']}").json()["state"] == "queued"  # locked until unloaded

        c.post(f"/jobs/{a['job_id']}/release")  # the contract's release is the unload
        assert c.get("/devices").json()[0]["plate"] is None
        time.sleep(0.2)
        assert c.get(f"/jobs/{b['job_id']}").json()["state"] == "queued"  # free is not ready
        load(c, "P1")
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
        once = run(c, "o", "preflight_once")  # fails attempt 1 only: a retry that succeeds
        assert once["state"] == "failed_preflight"
        assert run(c, "o", "preflight_once")["state"] == "done"
        wait(c, retry.json()["job_id"])
        c.post(f"/jobs/{retry.json()['job_id']}/release")

        for key, protocol, state in [("x", "protocol_fail", "failed_in_protocol"), ("e", "estop", "stopped")]:
            job = run(c, key, protocol)
            assert job["state"] == state and not job["stoppable"]
            repeat = c.post("/jobs", json=req(key, protocol))
            assert repeat.status_code == 200 and repeat.json()["job_id"] == job["job_id"]
            void_and_unload(c, job)


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
        void_and_unload(c, silent)
        assert wait(c, nxt["job_id"])["state"] == "done"  # the worker wasn't stuck on the silent job


def test_committed_openapi_matches_the_app():
    committed = json.loads((Path(__file__).parents[1] / "openapi.json").read_text())
    assert committed == json.loads(json.dumps(contract_spec(create_app(":memory:", [])))), \
        "regenerate: uv run python -m nf_lab_adapter.app > openapi.json"


# --- operator API: what a person does at the deck ---

def load(c: TestClient, plate: str, device: str = "fake-1"):
    return c.post(f"/devices/{device}/load", json={"plate": plate, "by": "ana"})


def test_a_job_runs_only_on_a_deck_holding_its_plate(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", operator=False) as c:
        p2 = c.post("/jobs", json=req("p2", plate="P2")).json()
        p1 = c.post("/jobs", json=req("p1")).json()
        time.sleep(0.2)
        assert c.get(f"/jobs/{p2['job_id']}").json()["state"] == "queued"  # empty deck: nothing runs
        assert load(c, "P1").json()["plate"] == "P1"
        assert wait(c, p1["job_id"])["state"] == "done"  # P1's job jumps the queue: its plate is loaded
        assert c.get(f"/jobs/{p2['job_id']}").json()["state"] == "queued"

        view = c.get("/devices").json()[0]
        assert (view["plate"], view["held"]["job_id"], view["status"]) == ("P1", p1["job_id"], "unload")
        assert [j["plate"] for j in c.get("/queue").json()] == ["P2"]
        assert load(c, "P2").status_code == 409  # P1 still on the deck
        assert c.post("/devices/fake-1/unload", json={"by": "ana"}).json()["held"] is None
        assert c.get(f"/jobs/{p1['job_id']}").json()["released_at"]
        load(c, "P2")
        assert wait(c, p2["job_id"])["state"] == "done"


def test_a_plate_is_on_one_device_at_most(tmp_path):
    devices = [Device(f"fake-{i}", f"cap-{i}", FakeBackend(0.05)) for i in (1, 2)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        assert load(c, "P1", "fake-1").status_code == 200
        r = load(c, "P1", "fake-2")
        assert r.status_code == 409 and "plate P1 is on fake-1" in r.json()["detail"]
        assert load(c, "P1", "fake-1").status_code == 200  # loading it again where it is: fine
        assert load(c, "P1", "nope").status_code == 404


def test_a_job_runs_on_the_pool_device_holding_its_plate(tmp_path):
    devices = [Device(f"fake-{i}", "liquid-handling", FakeBackend(0.05)) for i in (1, 2)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        load(c, "P2", "fake-2")
        job = c.post("/jobs", json=req("k", plate="P2")).json()
        assert job["device_id"] is None  # submit names a capability, not a device
        assert wait(c, job["job_id"])["device_id"] == "fake-2"  # fake-1 is free but not ready: empty deck


def test_a_pool_spreads_jobs_one_per_device_at_a_time(tmp_path):
    devices = [Device(f"fake-{i}", "liquid-handling", FakeBackend(0.1), simulated_operator=True) for i in (1, 2)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        jobs = [c.post("/jobs", json=req(f"k{i}", plate=f"P{i}")).json() for i in range(1, 5)]
        jobs = [wait(c, j["job_id"], lambda j: j["released_at"]) for j in jobs]
        assert {j["state"] for j in jobs} == {"done"}
        assert {j["device_id"] for j in jobs} == {"fake-1", "fake-2"}
        for d in ("fake-1", "fake-2"):
            runs = sorted((j["started_at"], j["released_at"]) for j in jobs if j["device_id"] == d)
            assert all(a[1] <= b[0] for a, b in zip(runs, runs[1:])), runs


def test_a_queue_routes_a_job_to_its_robots(tmp_path):
    devices = [Device(f"{q}-1", "liquid-handling", FakeBackend(0.05), queue=q) for q in ("opentrons", "hamilton")]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        load(c, "P1", "opentrons-1")
        job = c.post("/jobs", json=req("k", queue="hamilton")).json()
        assert (job["queue"], job["device_id"]) == ("hamilton", None)
        assert c.get("/queue").json()[0]["waiting_for"] == \
            "plate P1 is on opentrons-1, which isn't in queue hamilton: unload it there"
        c.post("/devices/opentrons-1/unload")
        load(c, "P1", "hamilton-1")
        assert wait(c, job["job_id"])["device_id"] == "hamilton-1"

        load(c, "P2", "opentrons-1")  # no queue: any robot of the capability
        assert wait(c, c.post("/jobs", json=req("k2", plate="P2")).json()["job_id"])["device_id"] == "opentrons-1"

        r = c.post("/jobs", json=req("k3", queue="tecan"))  # a typo fails at once instead of waiting forever
        assert r.status_code == 404 and "queue 'tecan'" in r.json()["detail"]
        # routing, not identity: the same key in another queue is the same job
        assert c.post("/jobs", json=req("k", queue="opentrons")).json()["job_id"] == job["job_id"]


def test_a_job_goes_only_to_robots_that_run_its_protocol(tmp_path):
    transfer_only = FakeBackend(0.05)
    transfer_only.PROTOCOLS = {"plate_transfer"}  # another brand: same capability, fewer protocols
    devices = [Device("fake-1", "liquid-handling", FakeBackend(0.05)), Device("fake-2", "liquid-handling", transfer_only)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        load(c, "P1", "fake-2")
        job = c.post("/jobs", json=req("k")).json()  # protocol ok: fake-1 only
        assert c.get("/queue").json()[0]["waiting_for"] == "plate P1 is on fake-2, which doesn't run ok: unload it there"
        c.post("/devices/fake-2/unload")
        load(c, "P1", "fake-1")
        assert wait(c, job["job_id"])["device_id"] == "fake-1"

        r = c.post("/jobs", json=req("k2", "nope"))  # no robot runs it: refused, not a pre-flight failure later
        assert r.status_code == 404 and "protocol 'nope'" in r.json()["detail"]


def test_the_simulated_operator_loads_a_plate_only_where_its_queue_runs(tmp_path):
    devices = [Device(f"{q}-1", "liquid-handling", FakeBackend(0.05), simulated_operator=True, queue=q)
               for q in ("opentrons", "hamilton")]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        jobs = [c.post("/jobs", json=req(f"k{i}", plate=f"P{i}", queue="hamilton")).json() for i in (1, 2)]
        assert {wait(c, j["job_id"], lambda j: j["released_at"])["device_id"] for j in jobs} == {"hamilton-1"}


def test_device_specs():
    assert device_specs("2", "fake") == [("fake-1", "liquid-handling", None), ("fake-2", "liquid-handling", None)]
    assert device_specs("liquid-handling/opentrons:2, liquid-handling/hamilton:1,thermocycling:1", "fake") == [
        ("opentrons-1", "liquid-handling", "opentrons"), ("opentrons-2", "liquid-handling", "opentrons"),
        ("hamilton-1", "liquid-handling", "hamilton"), ("thermocycling-1", "thermocycling", None)]
    for bad in ("liquid-handling", "liquid-handling:1,liquid-handling:1"):
        try:
            device_specs(bad, "fake")
        except ValueError as e:
            assert "NF_LAB_DEVICES" in str(e)
        else:
            raise AssertionError(bad)


def test_devices_and_queue_tell_a_newcomer_what_to_do(tmp_path):
    devices = [Device(f"fake-{i}", "liquid-handling", FakeBackend(0.05)) for i in (1, 2)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        status = lambda: {d["device_id"]: (d["status"], d["detail"]) for d in c.get("/devices").json()}
        assert "preflight_fail" in c.get("/devices").json()[0]["protocols"]  # what can I do here?
        assert status()["fake-1"] == ("empty", "empty deck: load a plate")

        a = c.post("/jobs", json=req("a")).json()
        assert c.get("/queue").json()[0]["waiting_for"] == "plate P1 isn't on any deck: load it on fake-1 or fake-2"
        load(c, "P1", "fake-1")
        wait(c, a["job_id"])
        assert status()["fake-1"] == ("unload", "done with plate P1: unload it")
        c.post("/jobs", json=req("b"))  # same plate again: it has to come off and back on
        assert c.get("/queue").json()[0]["waiting_for"] == \
            "plate P1 is on fake-1, done with plate P1: unload it, then load P1 again"

        load(c, "P2", "fake-2")
        wait(c, c.post("/jobs", json=req("c", "preflight_fail", plate="P2")).json()["job_id"])
        assert status()["fake-2"] == ("ready", "ready for plate P2; last job (plate P2) failed pre-flight: "
                                               "RuntimeError: deck check failed: tip rack missing")
        load(c, "P3", "fake-2")  # free (a pre-flight failure doesn't hold it): the plate swaps
        assert c.post("/jobs", json=req("d", plate="P4")).status_code == 201
        assert c.get("/queue").json()[1]["waiting_for"] == "plate P4 isn't on any deck: load it on fake-2"


def test_unload_and_load_wait_for_a_running_job(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", step=0.3, operator=False) as c:
        load(c, "P1")
        job = c.post("/jobs", json=req("k")).json()
        wait(c, job["job_id"], lambda j: j["state"] == "running")
        assert c.post("/devices/fake-1/unload").status_code == 409
        assert load(c, "P1").status_code == 409
        assert c.post("/devices/fake-1/estop").status_code == 200


def test_estop_stops_at_once_and_keeps_the_lock(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", step=0.3) as c:
        assert c.post("/devices/fake-1/estop").status_code == 409  # nothing running
        for key, past_boundary in [("before", False), ("after", True)]:
            job = c.post("/jobs", json=req(key)).json()
            wait(c, job["job_id"], lambda j: j["state"] == "running" and j["stoppable"] != past_boundary)
            c.post("/devices/fake-1/estop", json={"by": "ana"})
            job = wait(c, job["job_id"])
            assert (job["state"], job["reason"]) == ("stopped", "e-stop pressed by ana")
            assert c.get("/devices").json()[0]["held"]["job_id"] == job["job_id"]  # the operator leaves it for a person
            void_and_unload(c, job)


def test_resolve_as_done_reads_the_deck_and_the_retry_collects_it(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        job = wait(c, c.post("/jobs", json=req("y", "protocol_fail")).json()["job_id"])
        for unload in (f"/jobs/{job['job_id']}/release", "/devices/fake-1/unload"):  # not before it's resolved
            r = c.post(unload)
            assert r.status_code == 409 and "resolve it first (Succeeded or Failed)" in r.json()["detail"]
        assert c.get("/devices").json()[0]["status"] == "needs_person"
        assert c.post(f"/jobs/{job['job_id']}/resolve", json={"outcome": "maybe"}).status_code == 422
        done = c.post(f"/jobs/{job['job_id']}/resolve",
                      json={"outcome": "done", "by": "ana", "note": "all wells filled"}).json()
        assert (done["state"], done["result"], done["finished_at"]) == ("done", {"wells": 2}, job["finished_at"])
        assert done["reason"] == "resolved as done by ana: all wells filled; was failed_in_protocol: " \
                                 "RuntimeError: tip pickup failed mid-transfer"
        assert c.post(f"/jobs/{job['job_id']}/resolve", json={"outcome": "void"}).status_code == 409  # only failed jobs
        again = c.post("/jobs", json=req("y", "protocol_fail"))  # the task's retry or -resume: same key
        assert again.status_code == 200 and again.json()["job_id"] == job["job_id"]
        assert wait(c, job["job_id"], lambda j: j["released_at"])  # done: the simulated operator unloads it


def test_void_lets_the_key_run_again(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        job = run(c, "z", "protocol_fail")
        assert c.post("/jobs", json=req("z", "protocol_fail")).json()["job_id"] == job["job_id"]  # at-most-once
        void = c.post(f"/jobs/{job['job_id']}/resolve", json={"outcome": "void", "by": "ana", "note": "binned"}).json()
        assert void["state"] == "voided" and void["reason"].startswith("voided by ana: binned; was failed_in_protocol")
        assert wait(c, job["job_id"], lambda j: j["released_at"])  # voided: the simulated operator unloads it
        assert c.get("/lookup/z").json()["job_id"] == job["job_id"]
        retry = c.post("/jobs", json=req("z", "protocol_fail"))
        assert retry.status_code == 201 and retry.json()["attempt"] == 2
        assert c.get("/lookup/z").json()["job_id"] == retry.json()["job_id"]  # the live attempt wins


def test_simulated_operator_unloads_done_plates_and_loads_queued_ones(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        a = c.post("/jobs", json=req("a")).json()
        b = c.post("/jobs", json=req("b", plate="P2")).json()
        a, b = wait(c, a["job_id"]), wait(c, b["job_id"])
        assert a["state"] == b["state"] == "done" and a["released_at"] and b["released_at"]
        assert datetime.fromisoformat(b["started_at"]) > datetime.fromisoformat(a["released_at"])
        assert c.get("/devices").json()[0]["plate"] is None  # the last plate is unloaded too


def test_deck_page_is_served_and_the_operator_api_stays_out_of_the_contract(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        page = c.get("/deck")
        assert page.status_code == 200 and "Deck controls" in page.text
    spec = json.loads((Path(__file__).parents[1] / "openapi.json").read_text())
    assert set(spec["paths"]) == {"/jobs", "/jobs/{job_id}", "/lookup/{idempotency_key}",
                                  "/jobs/{job_id}/safe_stop", "/jobs/{job_id}/release"}
