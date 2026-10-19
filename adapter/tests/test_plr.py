from fastapi.testclient import TestClient

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.plr import PlrSimBackend
from test_adapter import req, wait


def adapter(ledger, step: float = 0.0) -> TestClient:
    return TestClient(create_app(str(ledger), [Device("plr-sim-1", "liquid-handling", PlrSimBackend(step))]))


def submit(c: TestClient, key: str, protocol: str = "plate_transfer", n: int = 10) -> dict:
    return c.post("/jobs", json=req(key, protocol, sample_ids=[f"S{i}" for i in range(1, n + 1)])).json()


def test_transfer_moves_liquid_column_by_column(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        job = wait(c, submit(c, "k")["job_id"])
        assert (job["state"], job["stoppable"]) == ("done", False)
        assert job["result"]["wells"]["S1"] == "A1" and job["result"]["wells"]["S9"] == "A2"  # 8 channels per column
        assert set(job["result"]["volume_ul"].values()) == {150.0}  # read back from the simulated wells


def test_safe_stop_cancels_before_first_aspirate_and_finishes_after(tmp_path):
    with adapter(tmp_path / "ledger.sqlite", step=0.2) as c:
        early = submit(c, "early")
        wait(c, early["job_id"], lambda j: j["state"] == "running" and j["stoppable"])  # picking up tips
        c.post(f"/jobs/{early['job_id']}/safe_stop")
        early = wait(c, early["job_id"])
        assert (early["state"], early["reason"], early["stoppable"]) == ("failed_preflight", "safe_stop", True)
        c.post(f"/jobs/{early['job_id']}/release")

        late = submit(c, "late")
        wait(c, late["job_id"], lambda j: not j["stoppable"])
        assert c.post(f"/jobs/{late['job_id']}/safe_stop").json()["stop_requested"]
        assert wait(c, late["job_id"])["state"] == "done"  # liquid moved: runs to completion


def test_preflight_failures_touch_nothing(tmp_path):
    with adapter(tmp_path / "ledger.sqlite") as c:
        for key, protocol, n in [("unknown", "pcr_setup", 2), ("too-many", "plate_transfer", 97)]:
            job = wait(c, submit(c, key, protocol, n)["job_id"])
            assert (job["state"], job["stoppable"]) == ("failed_preflight", True), job
            c.post(f"/jobs/{job['job_id']}/release")
