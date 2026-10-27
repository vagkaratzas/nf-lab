import asyncio

import pytest

from fastapi.testclient import TestClient

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.bindings import PCR, POSITIVE_CT, QPCR
from nf_lab_adapter.bindings.plr import PlrLiquidHandlerBackend, PlrPlateReaderBackend, PlrQpcrBackend, PlrThermocyclerBackend
from test_adapter import req, wait, wells


def adapter(ledger, step: float = 0.0) -> TestClient:
    return TestClient(create_app(str(ledger), [Device("plr-sim-1", "liquid-handling", PlrLiquidHandlerBackend(step),
                                                      simulated_operator=True)]))


def submit(c: TestClient, key: str, protocol: str = "plate_transfer", n: int = 10) -> dict:
    """n samples filled column by column from A1."""
    tokens = [f"{'ABCDEFGH'[i % 8]}{i // 8 + 1}:S{i + 1}" for i in range(n)]
    return c.post("/jobs", json=req(key, protocol, wells=wells(*tokens))).json()


def test_transfer_fills_the_plate_map_wells_from_their_source(tmp_path):
    backend = PlrLiquidHandlerBackend(0.0)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), [Device("plr-sim-1", "liquid-handling", backend)])) as c:
        plate_map = wells("A1:S1", "C1:S2", "D2:NC1:negative_control", "H12:BLK1:blank", "A12:S3")
        job = c.post("/jobs", json=req("k", "plate_transfer", plate="P7", wells=plate_map)).json()
        c.post("/devices/plr-sim-1/load", json={"plate": "P7"})  # a person loads it: the deck fills
        job = wait(c, job["job_id"])
        assert (job["state"], job["stoppable"], job["result"]["plate"]) == ("done", False, "P7")
        moved = {f"{w['row']}{w['column']}": (w["sample"], w["volume_ul"]) for w in job["result"]["wells"]}
        assert moved == {"A1": ("S1", 150.0), "C1": ("S2", 150.0), "D2": ("NC1", 150.0),
                         "A12": ("S3", 150.0), "H12": ("BLK1", 150.0)}  # read back from the simulated wells
        _, src, buf, dst, _, _ = backend.deck
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
        job = wait(c, submit(c, "off-plate", "plate_transfer", 97)["job_id"])  # 97th: A13
        assert (job["state"], job["stoppable"]) == ("failed_preflight", True), job
        r = c.post("/jobs", json=req("unknown", "pcr_setup"))  # no robot runs it: refused before any robot
        assert r.status_code == 404 and "runs protocol 'pcr_setup'" in r.json()["detail"]


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


def test_reader_reads_a_transferred_plate(tmp_path):
    devices = [Device("plr-sim-1", "liquid-handling", PlrLiquidHandlerBackend(0.0), simulated_operator=True),
               Device("plate-reading-1", "plate-reading", PlrPlateReaderBackend(0.0), simulated_operator=True)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        plate_map = wells("A1:S1", "D2:NC1:negative_control", "H12:BLK1:blank")
        transfer = wait(c, c.post("/jobs", json=req("t", "plate_transfer", wells=plate_map)).json()["job_id"])
        read = req("r", "plate_read", capability="plate-reading", wells=plate_map)
        read = wait(c, c.post("/jobs", json=read).json()["job_id"])
        assert (transfer["state"], read["state"], read["device_id"]) == ("done", "done", "plate-reading-1")
        values = {f"{w['row']}{w['column']}": w["absorbance"] for w in read["result"]["wells"]}
        assert values["D2"] == 0.05 and values["H12"] == 0.04 and 0.2 <= values["A1"] < 1.5
        assert read["result"]["wavelength_nm"] == 450


def test_reader_without_a_known_well_fails_preflight(tmp_path):
    devices = [Device("plate-reading-1", "plate-reading", PlrPlateReaderBackend(0.0), simulated_operator=True)]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), devices)) as c:
        job = c.post("/jobs", json=req("r", "plate_read", capability="plate-reading", wells=wells("A13:S1"))).json()
        assert wait(c, job["job_id"])["state"] == "failed_preflight"


def thermocycler(ledger, backend: PlrThermocyclerBackend | None = None) -> TestClient:
    devices = [Device("thermocycling-1", "thermocycling", backend or PlrThermocyclerBackend(0.0), simulated_operator=True)]
    return TestClient(create_app(str(ledger), devices))


def pcr(key: str, *tokens: str) -> dict:
    return req(key, "pcr", capability="thermocycling", wells=wells(*(tokens or ("A1:S1", "H12:BLK1:blank"))))


def test_pcr_runs_the_program_with_the_lid_open_after(tmp_path):
    backend = PlrThermocyclerBackend(0.0)
    with thermocycler(tmp_path / "ledger.sqlite", backend) as c:
        job = wait(c, c.post("/jobs", json=pcr("k")).json()["job_id"])
        assert (job["state"], job["stoppable"], job["device_id"]) == ("done", False, "thermocycling-1"), job["reason"]
        assert job["result"]["program"] == PCR and [w["sample"] for w in job["result"]["wells"]] == ["S1", "BLK1"]
        assert asyncio.run(backend.tc.get_lid_open())
        assert asyncio.run(backend.tc.get_total_step_count()) == PCR["cycles"] * 3 + 3  # + initial, final, hold


def test_pcr_safe_stop_before_cycling_leaves_the_plate_untouched_and_after_finishes(tmp_path):
    with thermocycler(tmp_path / "ledger.sqlite", PlrThermocyclerBackend(0.5)) as c:
        early = c.post("/jobs", json=pcr("early")).json()
        wait(c, early["job_id"], lambda j: j["state"] == "running" and j["stoppable"])  # closing the lid
        c.post(f"/jobs/{early['job_id']}/safe_stop")
        early = wait(c, early["job_id"])
        assert (early["state"], early["stoppable"]) == ("failed_preflight", True)
        c.post(f"/jobs/{early['job_id']}/release")

        late = c.post("/jobs", json=pcr("late")).json()
        wait(c, late["job_id"], lambda j: not j["stoppable"])  # cycling
        assert c.post(f"/jobs/{late['job_id']}/safe_stop").json()["stop_requested"]
        assert wait(c, late["job_id"], timeout=15)["state"] == "done"


def test_pcr_with_a_well_off_the_plate_fails_preflight(tmp_path):
    with thermocycler(tmp_path / "ledger.sqlite") as c:
        job = wait(c, c.post("/jobs", json=pcr("off", "A13:S1")).json()["job_id"])
        assert (job["state"], job["stoppable"]) == ("failed_preflight", True)


def test_qpcr_reverse_transcribes_cycles_and_calls_a_ct_per_well(tmp_path):
    backend = PlrQpcrBackend(0.0)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"),
                               [Device("qpcr-1", "qpcr", backend, simulated_operator=True)])) as c:
        plate = wells(*(f"A{i}:S{i}" for i in range(1, 9)), "H11:NTC1:negative_control", "H12:POS1:positive_control")
        job = wait(c, c.post("/jobs", json=req("q", "qpcr", capability="qpcr", wells=plate)).json()["job_id"])
        assert (job["state"], job["stoppable"]) == ("done", False), job["reason"]
        assert job["result"]["program"] == QPCR
        ct = {w["sample"]: w["ct"] for w in job["result"]["wells"]}
        assert ct["NTC1"] is None and ct["POS1"] == POSITIVE_CT
        assert all(c is None or 16 <= c < 34 for c in ct.values())
        assert asyncio.run(backend.tc.get_total_step_count()) == QPCR["cycles"] * 3 + 3


def test_split_fills_two_aliquots_from_one_aspirate(tmp_path):
    backend = PlrLiquidHandlerBackend(0.0)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"),
                               [Device("plr-sim-1", "liquid-handling", backend)])) as c:
        job = c.post("/jobs", json=req("s", "plate_split", wells=wells("A1:S1", "D2:NC1:negative_control"))).json()
        c.post("/devices/plr-sim-1/load", json={"plate": "P1"})  # a person loads it, and leaves it there
        job = wait(c, job["job_id"])
        assert job["state"] == "done", job["reason"]
        assert [[(w["sample"], w["volume_ul"]) for w in a["wells"]] for a in job["result"]["aliquots"]] == \
            [[("S1", 100.0), ("NC1", 100.0)]] * 2
        _, src, buf, _, _, _ = backend.deck
        assert src.get_item("A1").tracker.get_used_volume() == buf.get_item("D2").tracker.get_used_volume() == 100.0


@pytest.mark.parametrize("protocol, analyte", [("rna_extraction", "RNA"), ("dna_extraction", "DNA")])
def test_extraction_lyses_every_well_and_moves_the_eluate(tmp_path, protocol, analyte):
    backend = PlrLiquidHandlerBackend(0.0)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"),
                               [Device("plr-sim-1", "liquid-handling", backend)])) as c:
        plate = wells("A1:S1", "B1:S2", "D2:NC1:negative_control", "H12:BLK1:blank")
        job = c.post("/jobs", json=req("e", protocol, plate="P1-RNA", wells=plate)).json()
        c.post("/devices/plr-sim-1/load", json={"plate": "P1-RNA"})
        job = wait(c, job["job_id"])
        assert (job["state"], job["result"]["analyte"], job["result"]["plate"]) == ("done", analyte, "P1-RNA"), job["reason"]
        got = {w["sample"]: (w["eluate_ul"], w["yield_ng_ul"]) for w in job["result"]["wells"]}
        assert got["NC1"] == got["BLK1"] == (100.0, 0.0) and got["S1"][0] == 100.0 and 5 <= got["S1"][1] < 100
        assert got["S1"][1] != got["S2"][1]  # each sample its own yield
        _, src, buf, _, _, eluate = backend.deck
        assert src.get_item("D2").tracker.get_used_volume() == 250.0  # + 50 lysis - 100 lysate: controls are on the aliquot
        assert buf.get_item("D2").tracker.get_used_volume() == 250.0 and eluate.get_item("B2").tracker.get_used_volume() == 0
        assert eluate.get_item("D2").tracker.get_used_volume() == 100.0
