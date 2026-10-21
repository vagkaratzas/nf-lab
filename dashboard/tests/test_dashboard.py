import json
import sqlite3

from fastapi.testclient import TestClient

from nf_lab_dashboard.app import adapter_calls, main, nextflow_tasks

LOG = """\
Oct-21 09:06:31.454 [main] DEBUG nextflow.Session - Run name: gloomy_lagrange
Oct-21 09:06:32.877 [Task submitter] INFO  nextflow.Session - [d7/42293b] Submitted process > PLATE_TRANSFER (P1)
Oct-21 09:06:32.902 [Task submitter] INFO  nextflow.Session - [5e/bdcab2] Cached process > PLATE_TRANSFER (P2)
Oct-21 09:06:33.007 [Task monitor] DEBUG n.processor.TaskPollingMonitor - Task completed > TaskHandler[id: 1; name: PLATE_TRANSFER (P1); status: COMPLETED; exit: 75; error: -; workDir: /w
Oct-21 09:06:33.100 [Task submitter] INFO  nextflow.Session - [aa/111111] Re-submitted process > PLATE_TRANSFER (P1)
Oct-21 09:06:35.007 [Task monitor] DEBUG n.processor.TaskPollingMonitor - Task completed > TaskHandler[id: 3; name: PLATE_TRANSFER (P1); status: COMPLETED; exit: 0; error: -; workDir: /w
Oct-21 09:06:35.070 [Task submitter] INFO  nextflow.Session - [c8/d82d17] Submitted process > PLATE_REPORT (P1)
Oct-21 09:06:36.000 [main] DEBUG nextflow.Session - Session aborted -- Cause: boom
Oct-21 09:06:36.500 [main] DEBUG nextflow.cli.Launcher - Execution complete -- Goodbye
"""


def test_nextflow_log_gives_each_tasks_latest_state(tmp_path):
    (tmp_path / ".nextflow.log").write_text(LOG)
    run = nextflow_tasks(tmp_path / ".nextflow.log")
    assert (run["run_name"], run["state"]) == ("gloomy_lagrange", "aborted")  # an aborted run still says Goodbye
    assert [(t["name"], t["state"], t["attempts"], t["hash"]) for t in run["tasks"]] == [
        ("PLATE_TRANSFER (P1)", "done", 2, "aa/111111"),
        ("PLATE_TRANSFER (P2)", "cached", 0, "5e/bdcab2"),
        ("PLATE_REPORT (P1)", "submitted", 1, "c8/d82d17")]
    assert "no Nextflow log yet" in nextflow_tasks(tmp_path / "missing.log")["error"]


def ledger(path, *jobs):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (job_id TEXT, plate TEXT)")
    db.executemany("INSERT INTO jobs VALUES (?, ?)", jobs)
    db.commit()
    db.close()


def test_adapter_calls_name_the_plate_and_fold_status_polls(tmp_path):
    ledger(tmp_path / "ledger.sqlite", ("j1", "P1"), ("j2", "P2"))
    entries = [("POST", "/jobs", 201, "/jobs/j1"), ("GET", "/jobs/j1", 200, None), ("GET", "/jobs/j2", 200, None),
               ("GET", "/jobs/j1", 200, None), ("POST", "/devices/plr-sim-1/load", 200, None),
               ("POST", "/jobs/j1/release", 200, None), ("GET", "/jobs/j1", 200, None), ("POST", "/jobs", 404, None)]
    (tmp_path / "ledger.requests.jsonl").write_text("".join(
        json.dumps({"t": f"2026-10-21T09:00:0{i}+00:00", "method": m, "path": p, "status": s,
                    **({"location": loc} if loc else {})}) + "\n" for i, (m, p, s, loc) in enumerate(entries)))
    calls = adapter_calls(tmp_path / "ledger.requests.jsonl", tmp_path / "ledger.sqlite")
    assert [(c["op"], c["who"], c["http"], c["count"]) for c in calls] == [  # newest first
        ("submit", "", 404, 1), ("status", "plate P1", 200, 1), ("release", "plate P1", 200, 1),
        ("load", "plr-sim-1", 200, 1), ("status", "plate P2", 200, 1), ("status", "plate P1", 200, 2),
        ("submit", "plate P1", 201, 1)]


def test_endpoints_work_without_an_adapter_or_files(tmp_path, monkeypatch):
    monkeypatch.setenv("NF_LAB_ADAPTER", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setenv("NF_LAB_LEDGER", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("NF_LAB_NEXTFLOW_LOG", str(tmp_path / ".nextflow.log"))
    c = TestClient(main())
    assert "nf-lab live" in c.get("/").text
    assert c.get("/api/ledger").json() == [] and c.get("/api/adapter").json() == []
    assert "unreachable" in c.get("/api/robots").json()["error"]
    assert "error" in c.get("/api/nextflow").json()


def test_the_ledger_is_opened_read_only(tmp_path):
    from nf_lab_dashboard.app import ledger_rows
    ledger(tmp_path / "ledger.sqlite", ("j1", "P1"))
    try:
        ledger_rows(tmp_path / "ledger.sqlite", "DELETE FROM jobs")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e)
    else:
        raise AssertionError("the dashboard could write to the ledger")
