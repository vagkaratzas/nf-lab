"""Scenarios end to end on the fake backend. A stub stands in for Nextflow: it runs
labctl for four plates at once, like the example's wet tasks, and fails if one does."""

import asyncio
import socket
import sqlite3
from pathlib import Path

from nf_lab_dashboard.demo import Demo, Settings

NF_LAB = Path(__file__).parents[2]
STUB = """#!/usr/bin/env bash
pids=()
for p in P1 P2 P3 P4; do
    mkdir -p "$p"
    "{labctl}" run --wait --poll 0.1 --outdir "$p" --capability liquid-handling --protocol plate_transfer \\
        --protocol-version 1.0 --plate "$p" --wells "A1:S$p" > /dev/null 2>&1 & pids+=($!)
done
rc=0; for pid in "${{pids[@]}}"; do wait "$pid" || rc=1; done; exit $rc
"""


def run(tmp_path, scenario: str, step: float) -> tuple[Demo, list[tuple]]:
    stub = tmp_path / "nextflow"
    stub.write_text(STUB.format(labctl=NF_LAB / "adapter/.venv/bin/labctl"))
    stub.chmod(0o755)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    demo = Demo(Settings(nf_lab=NF_LAB, port=port, backend="fake", step=step, nextflow=[str(stub)], beat=0, tick=0.1,
                         runs=tmp_path / "runs"))

    async def go():
        demo.start(scenario)
        try:
            await asyncio.wait_for(asyncio.shield(demo.task), 90)
        finally:
            await demo.shutdown()
    asyncio.run(go())
    rows = sqlite3.connect(demo.run_dir / "ledger.sqlite").execute(
        "SELECT plate, device_id, state, attempt FROM jobs ORDER BY plate, attempt").fetchall()
    return demo, rows


def test_pool_spreads_four_plates_over_two_robots(tmp_path):
    demo, rows = run(tmp_path, "pool", step=0.1)
    assert demo.status == "finished", demo.log
    assert [(p, s) for p, _, s, _ in rows] == [("P1", "done"), ("P2", "done"), ("P3", "done"), ("P4", "done")]
    assert {d for _, d, _, _ in rows} == {"fake-1", "fake-2"}
    persons = [e["text"] for e in demo.log if e["kind"] == "person"]
    assert sum("(Load)" in t for t in persons) == 4 and sum("(Unload)" in t for t in persons) == 4


def test_estop_voids_one_plate_and_the_resume_runs_only_it_again(tmp_path):
    demo, rows = run(tmp_path, "estop", step=0.6)
    assert demo.status == "finished", demo.log
    stopped = [p for p, _, s, _ in rows if s == "voided"]
    assert len(stopped) == 1  # attempt 1 of the e-stopped plate
    assert [(s, a) for p, _, s, a in rows if p == stopped[0]] == [("voided", 1), ("done", 2)]
    assert sum(s == "done" for _, _, s, _ in rows) == 4 and len(rows) == 5  # nothing else ran twice
    texts = " ".join(e["text"] for e in demo.log)
    assert "presses E-stop" in texts and "They press Failed" in texts and "(exit 1)" in texts and "(exit 0)" in texts



def test_a_person_step_whose_moment_passed_presses_nothing(tmp_path):
    """The plate finished during the beat before E-stop: the re-check skips the press."""
    demo = Demo(Settings(nf_lab=NF_LAB, port=9, beat=0))  # nothing listens on port 9
    pressed = []

    async def api(method, path, body=None):
        if method == "POST":
            pressed.append(path)
        return [{"device_id": "r1", "status": "unload"}]
    demo.api = api
    moved_on = asyncio.run(demo.person("presses E-stop", "r1", "POST", "/devices/r1/estop",
                                       still=lambda ds: ds[0]["status"] == "running"))
    assert moved_on is None and pressed == [] and demo.log == []


def test_manual_leaves_the_deck_to_you_and_rerun_keeps_the_ledger(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stub = tmp_path / "nextflow"
    stub.write_text(STUB.format(labctl=NF_LAB / "adapter/.venv/bin/labctl"))
    stub.chmod(0o755)
    demo = Demo(Settings(nf_lab=NF_LAB, port=port, backend="fake", step=0.05, nextflow=[str(stub)], beat=0, tick=0.1,
                         runs=tmp_path / "runs"))

    async def go():
        demo.start("manual")
        while demo.pipeline is None:
            await asyncio.sleep(0.05)
        assert demo.log and all(e["kind"] == "say" for e in demo.log)  # the script presses nothing
        await demo.serve_until(demo.pipeline_ended)  # the test plays you, at the deck
        await asyncio.wait_for(asyncio.shield(demo.task), 30)
        assert demo.status == "finished" and demo.view()["can_rerun"]
        await demo._end(demo.adapter, 10)  # e.g. the dashboard restarted: Rerun brings the adapter back
        demo.rerun()
        await asyncio.wait_for(asyncio.shield(demo.task), 60)
        await demo.shutdown()
    asyncio.run(go())
    assert demo.status == "finished", demo.log
    assert "Rerun ended (exit 0)" in demo.log[-1]["text"]
    rows = sqlite3.connect(demo.run_dir / "ledger.sqlite").execute("SELECT state FROM jobs").fetchall()
    assert rows == [("done",)] * 4  # the ledger survived the restart: nothing ran twice
    assert (demo.run_dir / "nextflow.config").read_text() == "lineage.enabled = true\n"  # for `nextflow lineage`


def test_a_killed_dashboard_takes_its_adapter_with_it(tmp_path):
    """SIGKILL skips shutdown(); the adapter must not stay behind holding the port."""
    import os, signal, subprocess, sys, time
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    parent = subprocess.Popen([sys.executable, "-c", f"""
import asyncio, time
from pathlib import Path
from nf_lab_dashboard.demo import Demo, Settings
demo = Demo(Settings(nf_lab=Path({str(NF_LAB)!r}), port={port}, backend="fake", runs=Path({str(tmp_path)!r})))
demo.run_dir = demo.s.runs / "x"
asyncio.run(demo.fresh_adapter())
print(demo.adapter.pid, flush=True)
time.sleep(60)
"""], stdout=subprocess.PIPE, text=True)
    adapter = int(parent.stdout.readline())
    parent.send_signal(signal.SIGKILL)
    parent.wait()
    for _ in range(50):
        try:
            os.kill(adapter, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(adapter, signal.SIGKILL)
    raise AssertionError("the adapter outlived the dashboard")


ASSAY_STUB = """#!/usr/bin/env bash
echo "$@" > nextflow.args
run() {{ "{labctl}" run --wait --poll 0.1 --outdir "$1" --capability "$2" --protocol "$3" --protocol-version 1.0 \\
    --plate P1 --wells A1:S1 > /dev/null 2>&1; }}
run transfer liquid-handling plate_transfer && run read plate-reading plate_read
"""


def test_pool_and_estop_are_scripted_for_plate_transfer_only(tmp_path):
    demo = Demo(Settings(nf_lab=NF_LAB, runs=tmp_path))
    assert {e["id"]: e["scenarios"] for e in demo.view()["examples"]}["plate_assay"] == ["manual"]
    for scenario in ("pool", "estop"):
        try:
            demo.start(scenario, "plate_assay")
            raise AssertionError(f"{scenario} started for plate_assay")
        except KeyError:
            pass
    assert demo.task is None


def test_manual_plate_assay_moves_the_plate_from_the_liquid_handler_to_the_reader(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stub = tmp_path / "nextflow"
    stub.write_text(ASSAY_STUB.format(labctl=NF_LAB / "adapter/.venv/bin/labctl"))
    stub.chmod(0o755)
    demo = Demo(Settings(nf_lab=NF_LAB, port=port, backend="fake", step=0.05, nextflow=[str(stub)], beat=0, tick=0.1,
                         runs=tmp_path / "runs"))

    async def go():
        demo.start("manual", "plate_assay")
        while demo.pipeline is None:
            await asyncio.sleep(0.05)
        assert {d["capability"] for d in await demo.api("GET", "/devices")} == {"liquid-handling", "plate-reading"}
        await demo.serve_until(demo.pipeline_ended)  # the test plays you: P1 goes on a liquid handler, then the reader
        await asyncio.wait_for(asyncio.shield(demo.task), 30)
        await demo.shutdown()
    asyncio.run(go())
    assert demo.status == "finished", demo.log
    assert demo.run_dir == tmp_path / "runs/plate_assay/manual"
    assert "examples/plate_assay" in (demo.run_dir / "nextflow.args").read_text()
    rows = sqlite3.connect(demo.run_dir / "ledger.sqlite").execute(
        "SELECT protocol, device_id, state FROM jobs ORDER BY submitted_at").fetchall()
    assert [(p, d.split("-")[0], s) for p, d, s in rows] == [("plate_transfer", "liquid", "done"),
                                                             ("plate_read", "plate", "done")]


EXTRACTION_STUB = """#!/usr/bin/env bash
run() {{ "{labctl}" run --wait --poll 0.1 --outdir "$1" --capability "$2" --protocol "$3" --protocol-version 1.0 \\
    --plate "$4" --wells A1:S1 > /dev/null 2>&1; }}
run split liquid-handling plate_split P1 || exit 1
run rna liquid-handling rna_extraction P1-RNA & r=$!
run dna liquid-handling dna_extraction P1-DNA & d=$!
wait $r && wait $d && run pcr-rna thermocycling pcr P1-RNA && run pcr-dna thermocycling pcr P1-DNA
"""


def test_side_by_side_extracts_both_aliquots_at_once_then_runs_pcr(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stub = tmp_path / "nextflow"
    stub.write_text(EXTRACTION_STUB.format(labctl=NF_LAB / "adapter/.venv/bin/labctl"))
    stub.chmod(0o755)
    demo = Demo(Settings(nf_lab=NF_LAB, port=port, backend="fake", step=0.3, nextflow=[str(stub)], beat=0, tick=0.1,
                         runs=tmp_path / "runs"))

    async def go():
        demo.start("side_by_side", "parallel_extraction")
        try:
            await asyncio.wait_for(asyncio.shield(demo.task), 60)
        finally:
            await demo.shutdown()
    asyncio.run(go())
    assert demo.status == "finished", demo.log
    db = sqlite3.connect(demo.run_dir / "ledger.sqlite")
    rows = db.execute("SELECT protocol, plate, device_id, state FROM jobs ORDER BY submitted_at, protocol").fetchall()
    assert sorted((p, pl, s) for p, pl, _, s in rows) == [
        ("dna_extraction", "P1-DNA", "done"), ("pcr", "P1-DNA", "done"), ("pcr", "P1-RNA", "done"),
        ("plate_split", "P1", "done"), ("rna_extraction", "P1-RNA", "done")]
    extractions = db.execute("SELECT device_id, started_at, finished_at FROM jobs WHERE protocol LIKE '%_extraction'").fetchall()
    (d1, s1, f1), (d2, s2, f2) = extractions
    assert d1 != d2 and s1 < f2 and s2 < f1  # two robots, at the same time
    assert {d for p, _, d, _ in rows if p == "pcr"} == {"thermocycling-1"}


QPCR_STUB = """#!/usr/bin/env bash
run() {{ "{labctl}" run --wait --poll 0.1 --outdir "$1$2" --capability "$3" --protocol "$1" --protocol-version 1.0 \\
    --plate "$2" --wells A1:S1 H12:POS1:positive_control > /dev/null 2>&1; }}
pids=()
for p in Q01 Q02 Q03 Q04 Q05 Q06; do (run rna_extraction $p liquid-handling && run qpcr $p qpcr) & pids+=($!); done
rc=0; for pid in "${{pids[@]}}"; do wait "$pid" || rc=1; done; exit $rc
"""


def test_scale_runs_the_plates_over_both_pools_with_the_simulated_operator(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stub = tmp_path / "nextflow"
    stub.write_text(QPCR_STUB.format(labctl=NF_LAB / "adapter/.venv/bin/labctl"))
    stub.chmod(0o755)
    demo = Demo(Settings(nf_lab=NF_LAB, port=port, backend="fake", step=0.1, nextflow=[str(stub)], beat=0, tick=0.1,
                         runs=tmp_path / "runs"))

    async def go():
        demo.start("scale", "qpcr")
        try:
            await asyncio.wait_for(asyncio.shield(demo.task), 60)
        finally:
            await demo.shutdown()
    asyncio.run(go())
    assert demo.status == "finished", demo.log
    rows = sqlite3.connect(demo.run_dir / "ledger.sqlite").execute("SELECT protocol, device_id, state FROM jobs").fetchall()
    assert len(rows) == 12 and {s for *_, s in rows} == {"done"}
    assert len({d for p, d, _ in rows if p == "rna_extraction"}) == 4  # six plates queued at once: every liquid handler
    assert {d.split("-")[0] for p, d, _ in rows if p == "qpcr"} == {"qpcr"}
    assert not [e for e in demo.log if e["kind"] == "person"]  # the adapter moved the plates


TOUR_STUB = """#!/usr/bin/env bash
run() {{ "{labctl}" run --wait --poll 0.1 --outdir "$1$2" --capability "$3" --protocol "$1" --protocol-version 1.0 \\
    --plate "$2" --wells A1:S1 > /dev/null 2>&1; }}
pids=()
(run plate_scan PCR01 barcode-reading && run plate_seal PCR01 sealing && run pcr PCR01 thermocycling) & pids+=($!)
(run plate_mix CELL01 shaking && run plate_read CELL01 plate-reading) & pids+=($!)
(run powder_dispense CMPD01 powder-dispensing && run plate_weigh CMPD01 weighing) & pids+=($!)
rc=0; for pid in "${{pids[@]}}"; do wait "$pid" || rc=1; done; exit $rc
"""


def test_tour_runs_the_routines_over_one_robot_of_each_kind(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stub = tmp_path / "nextflow"
    stub.write_text(TOUR_STUB.format(labctl=NF_LAB / "adapter/.venv/bin/labctl"))
    stub.chmod(0o755)
    demo = Demo(Settings(nf_lab=NF_LAB, port=port, backend="fake", step=0.1, nextflow=[str(stub)], beat=0, tick=0.1,
                         runs=tmp_path / "runs"))

    async def go():
        demo.start("tour", "lab_tour")
        try:
            await asyncio.wait_for(asyncio.shield(demo.task), 60)
        finally:
            await demo.shutdown()
    asyncio.run(go())
    assert demo.status == "finished", demo.log
    rows = sqlite3.connect(demo.run_dir / "ledger.sqlite").execute("SELECT protocol, device_id, state FROM jobs").fetchall()
    assert len(rows) == 7 and {s for *_, s in rows} == {"done"}
    assert {d for _, d, _ in rows} == {"barcode-reading-1", "sealing-1", "thermocycling-1", "shaking-1", "plate-reading-1",
                                       "powder-dispensing-1", "weighing-1"}
    assert not [e for e in demo.log if e["kind"] == "person"]  # the adapter moved the plates
    assert any("7 of 17 robots have worked" in e["text"] for e in demo.log)
