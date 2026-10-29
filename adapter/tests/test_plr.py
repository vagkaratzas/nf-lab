import asyncio

import pytest

from fastapi.testclient import TestClient

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.bindings import PLATE_TARE_G, POSITIVE_CT, WELL_UL, resolve
from nf_lab_adapter.bindings.plr import MACHINES, PlrLiquidHandlerBackend, PlrPlateReaderBackend, PlrQpcrBackend, PlrThermocyclerBackend
from test_adapter import req, wait, wells

default = lambda protocol: resolve(protocol, {})  # the binding's program when no params are given


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
        assert job["result"]["program"] == default("pcr") and [w["sample"] for w in job["result"]["wells"]] == ["S1", "BLK1"]
        assert asyncio.run(backend.tc.get_lid_open())
        assert asyncio.run(backend.tc.get_total_step_count()) == default("pcr")["cycles"] * 3 + 3  # + initial, final, hold


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
        assert job["result"]["program"] == default("qpcr")
        ct = {w["sample"]: w["ct"] for w in job["result"]["wells"]}
        assert ct["NTC1"] is None and ct["POS1"] == POSITIVE_CT
        assert all(c is None or 16 <= c < 34 for c in ct.values())
        assert asyncio.run(backend.tc.get_total_step_count()) == default("qpcr")["cycles"] * 3 + 3


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


@pytest.mark.parametrize("capability, protocol", [("shaking", "plate_mix"), ("heating-shaking", "plate_lysis"),
                                                  ("temperature-control", "heat_inactivation"), ("tilting", "plate_tilt"),
                                                  ("centrifugation", "plate_spin"), ("incubation", "plate_incubate"),
                                                  ("sealing", "plate_seal"), ("peeling", "plate_unseal")])
def test_a_plate_holder_runs_its_program_past_the_boundary_and_a_safe_stop_before_it_touches_nothing(
        tmp_path, capability, protocol):
    backend = MACHINES[capability](0.05)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"),
                               [Device("d-1", capability, backend, simulated_operator=True)])) as c:
        job = lambda key: c.post("/jobs", json={**req(key, protocol), "capability": capability,
                                                "wells": wells("A1:S1", "B1:NC1:negative_control")}).json()
        early = job("early")
        wait(c, early["job_id"], lambda j: j["state"] == "running" and j["stoppable"])
        c.post(f"/jobs/{early['job_id']}/safe_stop")
        early = wait(c, early["job_id"])
        assert (early["state"], early["reason"], early["stoppable"]) == ("failed_preflight", "safe_stop", True)
        c.post(f"/jobs/{early['job_id']}/release")

        done = wait(c, job("late")["job_id"])
        assert (done["state"], done["stoppable"]) == ("done", False), done
        assert done["result"]["program"] == default(protocol)
        assert [w["sample"] for w in done["result"]["wells"]] == ["S1", "NC1"]


def test_the_incubator_takes_the_plate_in_only_past_preflight_and_hands_it_back_on_the_tray(tmp_path):
    backend = MACHINES["incubation"](0.05)
    tray, rack = lambda: backend.machine.loading_tray.resource, lambda: backend.machine.racks[0]
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), [Device("inc-1", "incubation", backend)])) as c:
        job = lambda key: c.post("/jobs", json={**req(key, "plate_incubate"), "capability": "incubation"}).json()
        early = job("early")
        c.post("/devices/inc-1/load", json={"plate": "P1"})  # a person puts it on the loading tray
        wait(c, early["job_id"], lambda j: j["state"] == "running" and j["stoppable"])
        c.post(f"/jobs/{early['job_id']}/safe_stop")
        assert wait(c, early["job_id"])["state"] == "failed_preflight"
        assert tray() is not None and len(rack().get_free_sites()) == len(rack().sites)  # never taken in
        c.post(f"/jobs/{early['job_id']}/release")

        late = job("late")
        wait(c, late["job_id"], lambda j: tray() is None)  # taken in: past the boundary
        assert c.get(f"/jobs/{late['job_id']}").json()["stoppable"] is False
        assert wait(c, late["job_id"])["state"] == "done"
        assert tray() is not None and len(rack().get_free_sites()) == len(rack().sites)  # back on the tray


@pytest.mark.parametrize("capability, protocol, crosses", [("pumping", "reagent_dispense", True),
                                                           ("powder-dispensing", "powder_dispense", True),
                                                           ("weighing", "plate_weigh", False),
                                                           ("barcode-reading", "plate_scan", False)])
def test_a_dispense_crosses_the_boundary_a_weighing_never_does(tmp_path, capability, protocol, crosses):
    backend = MACHINES[capability](0.5)  # a scan is stoppable for one step: wide enough for slow CI runners
    plate = wells("A1:S1", "B1:NC1:negative_control", "C2:S2", "H12:BLK1:blank")  # three columns
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"),
                               [Device("d-1", capability, backend, simulated_operator=True)])) as c:
        job = lambda key: c.post("/jobs", json={**req(key, protocol), "capability": capability, "wells": plate}).json()
        early = job("early")
        wait(c, early["job_id"], lambda j: j["state"] == "running" and j["stoppable"])
        c.post(f"/jobs/{early['job_id']}/safe_stop")
        assert wait(c, early["job_id"])["state"] == "failed_preflight"
        c.post(f"/jobs/{early['job_id']}/release")

        done = wait(c, job("late")["job_id"], timeout=30)
        assert (done["state"], done["stoppable"]) == ("done", not crosses), done
        result = done["result"]
        assert [w["sample"] for w in result["wells"]] == ["S1", "NC1", "S2", "BLK1"]
        if protocol == "reagent_dispense":  # read back from the plate: each well pumped once, column by column
            assert result["reagent"] == default(protocol)["reagent"]
            assert {w["volume_ul"] for w in result["wells"]} == {default(protocol)["volume_ul"]}
        elif protocol == "powder_dispense":
            assert result["powder"] == "NaCl" and {w["amount_mg"] for w in result["wells"]} == {default(protocol)["amount_mg"]}
        elif protocol == "plate_scan":
            assert result["barcode"] == "P1"
        else:
            assert result["weight_g"] == round(PLATE_TARE_G + 4 * WELL_UL / 1000, 3) == 45.6


def test_a_scanned_barcode_that_isnt_the_jobs_plate_fails_before_anything_happens(tmp_path):
    backend = MACHINES["barcode-reading"](0.0)
    load = backend.load

    async def wrong_plate(plate):  # a person confirms P1, but puts P7 in front of the scanner
        await load(plate)
        backend.scanner.backend.label = "P7"
    backend.load = wrong_plate
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), [Device("scan-1", "barcode-reading", backend)])) as c:
        job = c.post("/jobs", json={**req("s", "plate_scan"), "capability": "barcode-reading"}).json()
        c.post("/devices/scan-1/load", json={"plate": "P1"})
        job = wait(c, job["job_id"])
        assert (job["state"], job["stoppable"]) == ("failed_preflight", True)
        assert "scanned P7, but the job is for P1" in job["reason"], job


def run_with(tmp_path, capability: str, protocol: str, params: dict, backend=None, plate=("A1:S1", "B1:NC1:negative_control")):
    """One job with `params` on a simulated machine; returns (the done job, the backend)."""
    backend = backend or MACHINES[capability](0.0)
    tmp_path.mkdir(exist_ok=True)
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"),
                               [Device("d-1", capability, backend, simulated_operator=True)])) as c:
        r = c.post("/jobs", json={**req("p", protocol), "capability": capability, "wells": wells(*plate), "params": params})
        assert r.status_code == 201, r.json()
        job = wait(c, r.json()["job_id"], timeout=15)
        assert job["state"] == "done", job["reason"]
        assert job["program"] == {**default(protocol), **params}
        return job, backend


@pytest.mark.parametrize("capability, protocol, params", [
    ("shaking", "plate_mix", {"speed_rpm": 800, "duration_s": 30}),
    ("heating-shaking", "plate_lysis", {"temperature": 70.0, "speed_rpm": 900}),
    ("temperature-control", "heat_inactivation", {"temperature": 80.0}),
    ("tilting", "plate_tilt", {"angle_deg": 15.0}),
    ("centrifugation", "plate_spin", {"g": 500.0, "duration_s": 120}),
    ("incubation", "plate_incubate", {"temperature": 42.0}),
    ("sealing", "plate_seal", {"temperature": 170, "duration_s": 2.0}),
    ("peeling", "plate_unseal", {"adhere_time_s": 4.0}),
])
def test_a_plate_holder_runs_the_program_it_was_given(tmp_path, capability, protocol, params):
    job, _ = run_with(tmp_path, capability, protocol, params)
    assert job["result"]["program"] == job["program"]


def test_the_simulated_incubator_heats_to_the_programs_temperature(tmp_path):
    _, backend = run_with(tmp_path, "incubation", "plate_incubate", {"temperature": 42.0})
    assert asyncio.run(backend.machine.get_temperature()) == 42.0  # PyLabRobot's chatterbox would say 37.0


def test_the_thermocycler_runs_the_programs_cycles(tmp_path):
    job, backend = run_with(tmp_path, "thermocycling", "pcr", {"cycles": 10, "annealing_temperature": 55.0})
    assert asyncio.run(backend.tc.get_total_step_count()) == 10 * 3 + 3
    assert job["result"]["program"]["annealing_temperature"] == 55.0


def test_the_pump_and_the_liquid_handler_move_the_programs_volumes(tmp_path):
    job, _ = run_with(tmp_path / "pump", "pumping", "reagent_dispense", {"reagent": "master mix", "volume_ul": 20.0})
    assert job["result"]["reagent"] == "master mix" and {w["volume_ul"] for w in job["result"]["wells"]} == {20.0}
    job, backend = run_with(tmp_path / "lh", "liquid-handling", "plate_transfer", {"volume_ul": 80.0},
                            backend=PlrLiquidHandlerBackend(0.0))
    assert {w["volume_ul"] for w in job["result"]["wells"]} == {80.0}  # read back from the simulated wells
    job, backend = run_with(tmp_path / "split", "liquid-handling", "plate_split", {"volume_ul": 150.0},
                            backend=PlrLiquidHandlerBackend(0.0))
    assert [{w["volume_ul"] for w in a["wells"]} for a in job["result"]["aliquots"]] == [{150.0}, {150.0}]
    job, backend = run_with(tmp_path / "ex", "liquid-handling", "rna_extraction", {"lysis_ul": 60.0, "eluate_ul": 200.0},
                            backend=PlrLiquidHandlerBackend(0.0))
    assert {w["eluate_ul"] for w in job["result"]["wells"]} == {200.0}  # a full PCR well


def test_the_reader_and_the_powder_dispenser_follow_the_program(tmp_path):
    job, _ = run_with(tmp_path / "r", "plate-reading", "plate_read", {"wavelength_nm": 600})
    assert job["result"]["wavelength_nm"] == 600
    job, _ = run_with(tmp_path / "p", "powder-dispensing", "powder_dispense", {"powder": "KCl", "amount_mg": 2.5})
    assert job["result"]["powder"] == "KCl" and {w["amount_mg"] for w in job["result"]["wells"]} == {2.5}
