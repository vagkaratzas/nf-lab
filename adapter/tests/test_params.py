"""Protocol parameters: resolved at submit (binding defaults, the lab's limits, then params), refused with 422."""

import pytest
from fastapi.testclient import TestClient
from test_adapter import req, wait

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.bindings import load_limits
from nf_lab_adapter.bindings.fake import FakeBackend


def adapter(ledger, limits: dict | None = None) -> TestClient:
    devices = [Device("spin-1", "centrifugation", FakeBackend(0.01), simulated_operator=True),
               Device("pump-1", "pumping", FakeBackend(0.01), simulated_operator=True)]
    return TestClient(create_app(str(ledger), devices, limits=limits))


def spin(key: str, **kw) -> dict:
    return {**req(key, "plate_spin"), "capability": "centrifugation", **kw}


def dispense(key: str, **kw) -> dict:
    return {**req(key, "reagent_dispense"), "capability": "pumping", **kw}


def limits(tmp_path, text: str) -> dict:
    (tmp_path / "limits.toml").write_text(text)
    return load_limits(str(tmp_path / "limits.toml"))


def test_params_override_the_defaults_and_the_job_keeps_both(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        job = wait(c, c.post("/jobs", json=spin("k", params={"g": 500})).json()["job_id"])
        assert job["state"] == "done"
        assert job["params"] == {"g": 500}
        assert job["program"] == {"g": 500.0, "duration_s": 300}  # an int for a float param: coerced
        plain = wait(c, c.post("/jobs", json=spin("plain")).json()["job_id"])
        assert plain["params"] == {} and plain["program"] == {"g": 1000.0, "duration_s": 300}


@pytest.mark.parametrize("make, params, says", [
    (spin, {"gg": 500}, "has no param gg"),
    (spin, {"g": "500"}, "must be a number"),
    (spin, {"g": True}, "must be a number"),
    (spin, {"duration_s": 1.5}, "must be an integer"),
    (spin, {"g": 5000}, "within 10.0–4000.0"),
    (dispense, {"reagent": ""}, "non-blank string"),
    (dispense, {"reagent": "   "}, "non-blank string"),
    (dispense, {"reagent": 3}, "non-blank string"),
])
def test_a_bad_param_is_refused_and_nothing_is_queued(tmp_path, make, params, says):
    with adapter(tmp_path / "ledger.sqlite") as c:
        r = c.post("/jobs", json=make("k", params=params))
        assert r.status_code == 422 and says in r.json()["detail"], r.json()
        assert c.get("/lookup/k").status_code == 404


def test_a_non_finite_number_is_refused(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        for value in ("NaN", "Infinity", "-Infinity"):  # Python's JSON reads these; the resolver mustn't take them
            body = f'{{"capability": "centrifugation", "protocol": "plate_spin", "protocol_version": "1.0", ' \
                   f'"plate": "P1", "wells": [{{"row": "A", "column": 1, "sample": "S1"}}], ' \
                   f'"idempotency_key": "k", "params": {{"g": {value}}}}}'
            r = c.post("/jobs", content=body, headers={"Content-Type": "application/json"})
            assert r.status_code == 422, (value, r.text)
        assert c.get("/lookup/k").status_code == 404


def test_a_protocol_without_params_refuses_any(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        r = c.post("/jobs", json={**spin("k"), "capability": "pumping", "protocol": "ok", "params": {"x": 1}})
        assert r.status_code == 422 and "it takes none" in r.json()["detail"]


def test_the_lab_narrows_fixes_and_sets_defaults(tmp_path):
    lab = limits(tmp_path, """
[plate_spin.g]
max = 500.0
default = 400
[plate_spin.duration_s]
fixed = 120
[reagent_dispense.reagent]
choices = ["master mix", "DMSO"]
default = "master mix"
""")
    with adapter(tmp_path / "ledger.sqlite", lab) as c:
        r = c.post("/jobs", json=spin("over", params={"g": 800}))
        assert r.status_code == 422 and "within 10.0–500.0" in r.json()["detail"]
        r = c.post("/jobs", json=spin("fixed", params={"duration_s": 60}))
        assert r.status_code == 422 and "fixed at 120" in r.json()["detail"]
        r = c.post("/jobs", json=dispense("choice", params={"reagent": "wash buffer"}))
        assert r.status_code == 422 and "one of master mix, DMSO" in r.json()["detail"]
        job = c.post("/jobs", json=spin("ok", params={"duration_s": 120})).json()  # the fixed value itself: fine
        assert job["program"] == {"g": 400.0, "duration_s": 120}
        assert c.post("/jobs", json=dispense("mix")).json()["program"]["reagent"] == "master mix"


def test_a_live_job_keeps_its_program_after_the_lab_tightens_its_limits(tmp_path):
    ledger = tmp_path / "ledger.sqlite"
    first = "a" * 32
    with adapter(ledger) as c:
        done = wait(c, c.post("/jobs", json=spin("k", params={"g": 800}, task_hash=first, session_id="s1")).json()["job_id"])
        assert done["state"] == "done"
    with adapter(ledger, limits(tmp_path, "[plate_spin.g]\nmax = 500.0\n")) as c:
        again = c.post("/jobs", json=spin("k", params={"g": 800}, task_hash="b" * 32, session_id="s2"))  # -resume
        assert again.status_code == 200 and again.json()["job_id"] == done["job_id"]
        assert again.json()["program"]["g"] == 800.0
        assert [j["job_id"] for j in c.get("/jobs", params={"session": "s2"}).json()] == [done["job_id"]]
        assert c.post("/jobs", json=spin("new", params={"g": 800})).status_code == 422  # a new job: the new limit


def test_a_key_reused_with_other_params_is_a_conflict(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        assert c.post("/jobs", json=spin("k", params={"g": 500})).status_code == 201
        assert c.post("/jobs", json=spin("k", params={"g": 600})).status_code == 409


@pytest.mark.parametrize("text, says", [
    ("[plate_spin.g]\nmax = nan\n", "must be finite"),
    ("[plate_spin.g]\nmin = inf\n", "must be finite"),
    ("[plate_spin.g]\nmin = 600.0\nmax = 500.0\n", "is above max"),
    ("[plate_spin.g]\nmax = 9000.0\n", "within 10.0–4000.0"),
    ("[plate_spin.g]\nmax = 500.0\ndefault = 800.0\n", "within 10.0–500.0"),
    ("[plate_spin.g]\nfixed = 400.0\ndefault = 300.0\n", "default and fixed differ"),
    ("[plate_spin.duration_s]\nmax = 1.5\n", "must be an integer"),
    ("[plate_spin.g]\nstep = 10\n", "unknown key step"),
    ("[plate_spin.gg]\nmax = 1.0\n", "has no param 'gg'"),
    ("[no_such_protocol.g]\nmax = 1.0\n", "isn't a protocol table"),
    ("plate_spin = 1\n", "isn't a protocol table"),
    ("[reagent_dispense.reagent]\nmax = 1\n", "min and max are for numbers"),
    ("[reagent_dispense.reagent]\nchoices = []\n", "non-empty list"),
    ('[reagent_dispense.reagent]\nchoices = ["DMSO", " "]\n', "non-blank string"),
    ('[reagent_dispense.reagent]\ndefault = ""\n', "non-blank string"),
    ('[qpcr.channel]\nfixed = "Texas Red"\n', "one of FAM, HEX, ROX, Cy5"),
    ("[plate_spin.g]\nchoices = [1.0]\n", "choices are for strings"),
    ("[plate_spin\n", "Expected"),
])
def test_a_bad_limits_file_is_refused(tmp_path, text, says):
    with pytest.raises(ValueError, match=None) as e:
        limits(tmp_path, text)
    assert says in str(e.value), e.value


def test_a_limit_inside_the_machines_is_kept_as_the_lab_wrote_it(tmp_path):
    assert limits(tmp_path, "[pcr.cycles]\ndefault = 35\n[plate_spin.g]\nmin = 100\n") == {
        "pcr": {"cycles": {"default": 35}}, "plate_spin": {"g": {"min": 100.0}}}
