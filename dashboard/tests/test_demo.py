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
