"""Dashboard: a live view of one lab run, for demos. Read-only.

Run:  uv run uvicorn nf_lab_dashboard.app:main --factory --port 8080

Four panels, each one JSON endpoint the page polls every second:
- /api/nextflow  tasks from the run's .nextflow.log (not the trace file: it gets a
                 row only when a task completes or is cached)
- /api/adapter   the calls the adapter received, from its request log
- /api/ledger    the adapter's jobs, from its SQLite ledger, opened read-only
- /api/robots    each robot's status and lock, from the adapter's GET /devices

It never imports adapter code: it reads the adapter's files and its HTTP API.
Settings (environment):
  NF_LAB_ADAPTER        adapter URL (default http://127.0.0.1:8000)
  NF_LAB_LEDGER         the adapter's ledger (default adapter/ledger.sqlite); the
                        request log is next to it (<ledger>.requests.jsonl)
  NF_LAB_NEXTFLOW_LOG   the run's log (default examples/plate_transfer/.nextflow.log)
"""

import json
import os
import re
import sqlite3
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

NF_LAB = Path(__file__).parents[3]  # dashboard/src/nf_lab_dashboard/app.py -> nf-lab

SUBMIT = re.compile(r"\[(\w\w/\w{6})\] (Submitted|Re-submitted|Cached) process > (.+)$")
DONE = re.compile(r"Task completed > TaskHandler\[id: \d+; name: (.+?); status: (\w+); exit: (\S+);")
RUN_NAME = re.compile(r"Run name: (\S+)")
ENDED = {"Execution complete -- Goodbye": "finished", "Session aborted": "aborted"}

# adapter request -> contract operation (or operator action) name
OPS = [("POST", r"/jobs", "submit"), ("GET", r"/jobs/[^/]+", "status"), ("GET", r"/lookup/[^/]+", "lookup"),
       ("POST", r"/jobs/[^/]+/safe_stop", "safe_stop"), ("POST", r"/jobs/[^/]+/release", "release"),
       ("POST", r"/jobs/[^/]+/resolve", "resolve"), ("POST", r"/devices/[^/]+/(load|unload|estop)", None)]


def nextflow_tasks(log: Path) -> dict:
    """Run name, run state, and each task's latest state, in submit order."""
    if not log.exists():
        return {"error": f"no Nextflow log yet: {log}"}
    run = {"log": str(log), "run_name": None, "state": "running", "tasks": {}}
    for line in log.read_text(errors="replace").splitlines():
        time = line[7:15]  # "Oct-21 09:06:32.877 ..." -> 09:06:32
        if m := SUBMIT.search(line):
            hash_, how, name = m.groups()
            task = run["tasks"].setdefault(name, {"name": name, "attempts": 0})
            task.update(hash=hash_, time=time, state="cached" if how == "Cached" else "submitted")
            task["attempts"] += how != "Cached"
        elif m := DONE.search(line):
            name, status, exit_ = m.groups()
            if task := run["tasks"].get(name):
                task.update(time=time, state="done" if exit_ == "0" else f"exit {exit_}" if exit_ != "-" else status.lower())
        elif m := RUN_NAME.search(line):
            run["run_name"] = m.group(1)
        elif run["state"] != "aborted":  # an aborted run still says Goodbye
            run["state"] = next((s for k, s in ENDED.items() if k in line), run["state"])
    run["tasks"] = list(run["tasks"].values())
    return run


def ledger_rows(ledger: Path, query: str, args=()) -> list[dict]:
    if not ledger.exists():
        return []
    db = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)  # read-only: the adapter owns the ledger
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(query, args)]
    finally:
        db.close()


def adapter_calls(log: Path, ledger: Path, limit: int = 40) -> list[dict]:
    """The latest calls, newest first. A job's status polls fold into one row with a count, until
    another call for that job: jobs poll side by side, so their polls interleave."""
    if not log.exists():
        return []
    with log.open("rb") as f:
        start = max(0, f.seek(0, 2) - 200_000)  # the tail is enough
        f.seek(start)
        lines = f.read().decode(errors="replace").splitlines()[1 if start else 0:]  # drop a cut first line
    try:
        plates = {r["job_id"]: r["plate"] for r in ledger_rows(ledger, "SELECT job_id, plate FROM jobs")}
    except sqlite3.Error:  # the ledger panel reports it
        plates = {}
    calls: list[dict] = []
    polling: dict[str, dict] = {}  # who -> its status row still counting polls
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = next((name or m.group(1) for method, pattern, name in OPS
                   if e["method"] == method and (m := re.fullmatch(pattern, e["path"]))), e["path"])
        target = e.get("location", e["path"])
        job = target.split("/")[2] if target.startswith("/jobs/") else None
        who = f"plate {plates[job]}" if job in plates else target.split("/")[2] if target.startswith("/devices/") else ""
        call = {"time": e["t"][11:19], "op": op, "who": who, "http": e["status"], "count": 1}
        if op == "status" and (row := polling.get(who)) and row["http"] == e["status"]:
            row.update(time=call["time"], count=row["count"] + 1)
            continue
        calls.append(call)
        if op == "status":
            polling[who] = call
        else:
            polling.pop(who, None)
    return calls[-limit:][::-1]


def robots(adapter: str, ledger: Path) -> dict:
    try:
        with urllib.request.urlopen(f"{adapter}/devices", timeout=1) as r:
            devices = json.load(r)
    except OSError as e:
        return {"error": f"adapter unreachable at {adapter}: {e}"}
    out = []
    for d in devices:
        held, heartbeat = d["held"], None
        if held and held["device_heartbeat_at"]:
            heartbeat = round((datetime.now(UTC) - datetime.fromisoformat(held["device_heartbeat_at"])).total_seconds(), 1)
        out.append({"device_id": d["device_id"], "status": d["status"], "detail": d["detail"], "plate": d["plate"],
                    "viewer": d["viewer"], "heartbeat_s": heartbeat,
                    "lock": f"plate {held['plate']}, job {held['job_id'][:8]} ({held['state']})" if held else None,
                    "liquid_moved": bool(held and not held["stoppable"])})
    return {"robots": out}


def main() -> FastAPI:
    adapter = os.environ.get("NF_LAB_ADAPTER", "http://127.0.0.1:8000").rstrip("/")
    ledger = Path(os.environ.get("NF_LAB_LEDGER", NF_LAB / "adapter/ledger.sqlite")).resolve()
    nextflow_log = Path(os.environ.get("NF_LAB_NEXTFLOW_LOG", NF_LAB / "examples/plate_transfer/.nextflow.log")).resolve()
    request_log = ledger.with_suffix(".requests.jsonl")

    app = FastAPI(title="nf-lab dashboard")

    @app.get("/", include_in_schema=False)
    async def page() -> HTMLResponse:
        return HTMLResponse((Path(__file__).parent / "index.html").read_text())

    @app.get("/api/config")
    async def config() -> dict:
        return {"adapter": adapter, "ledger": str(ledger), "nextflow_log": str(nextflow_log)}

    @app.get("/api/nextflow")
    async def nextflow() -> dict:
        return nextflow_tasks(nextflow_log)

    @app.get("/api/adapter")
    async def calls() -> list[dict]:
        return adapter_calls(request_log, ledger)

    @app.get("/api/ledger")
    async def jobs() -> list[dict] | dict:
        hint = "set NF_LAB_LEDGER to the file the adapter uses (the adapter's NF_LAB_LEDGER, as an absolute path)"
        if not ledger.exists():
            return {"error": f"no ledger at {ledger}: {hint}"}
        try:
            return ledger_rows(ledger, """SELECT job_id, plate, protocol, device_id, state, attempt, stoppable, reason,
                                                 submitted_at, started_at, finished_at, released_at
                                          FROM jobs ORDER BY rowid DESC LIMIT 20""")
        except sqlite3.Error as e:  # e.g. an old ledger the adapter would refuse
            return {"error": f"can't read the ledger {ledger} ({e}): {hint}"}

    @app.get("/api/robots")
    def devices() -> dict:  # sync: the adapter call blocks, so FastAPI runs it in a thread
        return robots(adapter, ledger)

    return app
