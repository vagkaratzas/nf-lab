from fastapi.testclient import TestClient

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.plr import PlrSimBackend
from test_adapter import req, wait, wells


def adapter(ledger, step: float = 0.0) -> TestClient:
    return TestClient(create_app(str(ledger), [Device("plr-sim-1", "liquid-handling", PlrSimBackend(step),
                                                      simulated_operator=True)]))


def submit(c: TestClient, key: str, protocol: str = "plate_transfer", n: int = 10) -> dict:
    """n samples filled column by column from A1."""
    tokens = [f"{'ABCDEFGH'[i % 8]}{i // 8 + 1}:S{i + 1}" for i in range(n)]
    return c.post("/jobs", json=req(key, protocol, wells=wells(*tokens))).json()


def test_transfer_fills_the_plate_map_wells_from_their_source(tmp_path):
    backend = PlrSimBackend(0.0)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), [Device("plr-sim-1", "liquid-handling", backend)])) as c:
        plate_map = wells("A1:S1", "C1:S2", "D2:NC1:negative_control", "H12:BLK1:blank", "A12:S3")
        job = c.post("/jobs", json=req("k", "plate_transfer", plate="P7", wells=plate_map)).json()
        c.post("/devices/plr-sim-1/load", json={"plate": "P7"})  # a person loads it: the deck fills
        job = wait(c, job["job_id"])
        assert (job["state"], job["stoppable"], job["result"]["plate"]) == ("done", False, "P7")
        moved = {f"{w['row']}{w['column']}": (w["sample"], w["volume_ul"]) for w in job["result"]["wells"]}
        assert moved == {"A1": ("S1", 150.0), "C1": ("S2", 150.0), "D2": ("NC1", 150.0),
                         "A12": ("S3", 150.0), "H12": ("BLK1", 150.0)}  # read back from the simulated wells
        _, src, buf, dst = backend.deck
        assert dst.get_item("B1").tracker.get_used_volume() == 0  # a gap stays empty
        assert src.get_item("D2").tracker.get_used_volume() == 300.0  # a control comes from the buffer plate
        assert buf.get_item("D2").tracker.get_used_volume() == 150.0
        c.post("/devices/plr-sim-1/unload")
        assert dst.get_item("A1").tracker.get_used_volume() == src.get_item("A2").tracker.get_used_volume() == 0


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
        for key, protocol, n in [("unknown", "pcr_setup", 2), ("off-plate", "plate_transfer", 97)]:  # 97th: A13
            job = wait(c, submit(c, key, protocol, n)["job_id"])
            assert (job["state"], job["stoppable"]) == ("failed_preflight", True), job
            c.post(f"/jobs/{job['job_id']}/release")


def test_a_reloaded_deck_runs_after_an_estop_mid_transfer(tmp_path):
    """The e-stop leaves liquid in the tips; the person's unload and load give fresh tips."""
    with adapter(tmp_path / "ledger.sqlite", step=0.05) as c:
        plate = wells("A1:S1", "G1:NC1:negative_control", "H1:BLK1:blank")
        job = c.post("/jobs", json=req("k", "plate_transfer", wells=plate)).json()
        wait(c, job["job_id"], lambda j: not j["stoppable"])  # liquid in the tips
        c.post("/devices/plr-sim-1/estop")
        assert wait(c, job["job_id"])["state"] == "stopped"
        c.post(f"/jobs/{job['job_id']}/resolve", json={"outcome": "void"})
        c.post("/devices/plr-sim-1/unload")  # the simulated operator loads P1 again for the retry
        retry = wait(c, c.post("/jobs", json=req("k", "plate_transfer", wells=plate)).json()["job_id"])
        assert (retry["state"], retry["attempt"]) == ("done", 2), retry["reason"]
        assert {w["volume_ul"] for w in retry["result"]["wells"]} == {150.0}
