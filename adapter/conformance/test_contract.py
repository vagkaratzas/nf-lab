"""The device-job contract's guarantees, checked through its operations only (see conftest.py).

Backend-agnostic: no scripted failures, no timing assumptions. Where a reply depends on timing
(a safe_stop before or after the stop boundary), the check holds for every outcome."""

import time
import uuid

import pytest

FINAL = {"done", "failed_preflight", "failed_in_protocol", "stopped", "voided"}


def new_request(opts: dict, **kw) -> dict:
    """A one-plate job no earlier run used: its own key and plate."""
    tag = uuid.uuid4().hex[:12]
    return {"capability": opts["capability"], "protocol": opts["protocol"], "protocol_version": "1.0",
            "plate": f"CK-{tag}", "idempotency_key": f"conformance-{tag}",
            "wells": [{"row": "A", "column": 1, "sample": "S1", "type": "sample"},
                      {"row": "B", "column": 1, "sample": "S2", "type": "sample"},
                      {"row": "H", "column": 12, "sample": "NC1", "type": "negative_control"}], **kw}


def finish(api, opts: dict, job_id: str) -> dict:
    deadline = time.monotonic() + opts["job_timeout"]
    while (job := api("status", job_id=job_id).body)["state"] not in FINAL:
        assert time.monotonic() < deadline, \
            f"job {job_id} still {job['state']} after {opts['job_timeout']} s: does the adapter load queued plates?"
        time.sleep(0.1)
    return job


def test_a_key_is_one_physical_job(api, opts):
    req = new_request(opts)
    first, again = api("submit", req), api("submit", req)
    assert (first.status, again.status) == (201, 200)
    job_id = first.body["job_id"]
    assert again.body["job_id"] == job_id and first.body["attempt"] == 1
    assert first.headers.get("location") == f"/jobs/{job_id}"
    assert api("submit", {**req, "wells": req["wells"][::-1]}).status == 200, "listing the wells in another order made a new job"
    assert api("submit", {**req, "plate": req["plate"] + "-other"}).status == 409

    done = finish(api, opts, job_id)
    assert done["state"] == "done" and done["result"] is not None and done["device_id"]
    after = api("submit", req)  # a resume or a fresh launch: the stored job, nothing runs again
    assert after.status == 200 and after.body["job_id"] == job_id and after.body["state"] == "done"
    assert api("lookup", idempotency_key=req["idempotency_key"]).body["job_id"] == job_id
    api("release", job_id=job_id)


def test_bad_requests_and_unknown_ids_are_refused(api, opts):
    a1 = {"row": "A", "column": 1, "sample": "S1", "type": "sample"}
    for wells in [[a1, {**a1, "sample": "S2"}], [{**a1, "row": "Q"}], [{**a1, "column": 25}],
                  [{**a1, "type": "standard"}], []]:
        assert api("submit", new_request(opts, wells=wells)).status == 422, wells
    assert api("submit", new_request(opts, task_hash="not-a-task-hash")).status == 422
    assert api("submit", new_request(opts, capability="no-such-capability")).status == 404
    unknown = uuid.uuid4().hex
    assert api("status", job_id=unknown).status == 404
    assert api("lookup", idempotency_key=unknown).status == 404
    assert api("safe_stop", job_id=unknown).status == 404
    assert api("release", job_id=unknown).status == 404


def test_a_device_runs_one_job_at_a_time(api, opts):
    ids = [api("submit", new_request(opts)).body["job_id"] for _ in range(4)]
    jobs = [finish(api, opts, i) for i in ids]
    for i in ids:
        api("release", job_id=i)
    assert all(j["state"] == "done" and j["device_id"] for j in jobs)
    by_device: dict[str, list[dict]] = {}
    for j in jobs:
        by_device.setdefault(j["device_id"], []).append(j)
    for device, runs in by_device.items():
        runs.sort(key=lambda j: j["started_at"])
        for a, b in zip(runs, runs[1:]):
            assert a["finished_at"] <= b["started_at"], f"{device} ran {a['job_id']} and {b['job_id']} at once"


def test_safe_stop_never_repeats_material_work(api, opts):
    reqs = [new_request(opts) for _ in range(3)]
    ids = [api("submit", r).body["job_id"] for r in reqs]
    api("safe_stop", job_id=ids[-1])  # likely still queued behind the others; any phase must be safe
    stopped = finish(api, opts, ids[-1])
    for i in ids[:-1]:
        finish(api, opts, i)
    if stopped["state"] == "failed_preflight":  # stopped before the boundary: nothing consumed
        assert stopped["stoppable"]
        retry = api("submit", reqs[-1])
        assert retry.status == 201 and retry.body["attempt"] == 2
        assert finish(api, opts, retry.body["job_id"])["state"] == "done"
        ids.append(retry.body["job_id"])
    else:  # past the boundary: it runs to the end
        assert stopped["state"] == "done" and stopped["result"] is not None
    final = api("safe_stop", job_id=ids[0])
    assert final.status == 200 and final.body["state"] == "done", "safe_stop changed a finished job"
    for i in ids:
        api("release", job_id=i)


def test_release_unlocks_only_finished_jobs_and_is_idempotent(api, opts):
    job_id = api("submit", new_request(opts)).body["job_id"]
    early = api("release", job_id=job_id)
    assert early.status == 409 or early.body["state"] in FINAL
    finish(api, opts, job_id)
    first, again = api("release", job_id=job_id), api("release", job_id=job_id)
    assert first.status == again.status == 200
    assert first.body["released_at"] and again.body["released_at"] == first.body["released_at"]


def test_a_session_lists_its_jobs_with_the_tasks_that_collected_them(api, opts):
    session, h1, h2 = f"conformance-{uuid.uuid4().hex}", uuid.uuid4().hex, uuid.uuid4().hex
    req = new_request(opts, session_id=session)
    job_id = api("submit", {**req, "task_hash": h1}).body["job_id"]
    assert api("submit", {**req, "task_hash": h2}).status == 200  # a retry or resume: another task, same job
    jobs = api("session_jobs", query={"session": session}).body
    assert [(j["job_id"], [t["task_hash"] for t in j["collected_by"]]) for j in jobs] == [(job_id, [h1, h2])]
    assert api("session_jobs", query={"session": uuid.uuid4().hex}).body == []
    finish(api, opts, job_id)
    api("release", job_id=job_id)


def test_every_operation_in_the_spec_was_checked(api, request):
    """Keep last: a new operation in openapi.json needs a check here."""
    this = request.node.callspec.id
    selected = {i.originalname for i in request.session.items if i.module is request.module and i.callspec.id == this}
    if selected != {name for name in vars(request.module) if name.startswith("test_")}:
        pytest.skip("only some checks selected (-k, -m or test ids)")
    assert api.called == set(api.ops), f"not checked: {sorted(set(api.ops) - api.called)}"
