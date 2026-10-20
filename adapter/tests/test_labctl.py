import json
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.backends import FakeBackend
from nf_lab_adapter.labctl import Client


@contextmanager
def serve(tmp_path, step: float = 0.05):
    """The adapter on a real port, since labctl talks HTTP. Yields (url, stop)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(str(tmp_path / "ledger.sqlite"), [Device("fake-1", "liquid-handling", FakeBackend(step))])
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)

    def stop():
        server.should_exit = True
        thread.join()

    yield f"http://127.0.0.1:{port}", stop
    if thread.is_alive():
        stop()


def labctl(url: str, protocol: str = "ok", key: str | None = None, grace: float = 2, wait=True,
           out: Path | None = None, **popen):
    cmd = [sys.executable, "-m", "nf_lab_adapter.labctl", "run", "--capability", "liquid-handling",
           "--protocol", protocol, "--protocol-version", "1.0", "--samples", "S1", "S2",
           "--adapter", url, "--poll", "0.05", "--grace", str(grace),
           "--outdir", str(out or tempfile.mkdtemp())]
    cmd += ["--key", key] if key else []
    cmd += ["--wait"] if wait else []
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **popen)


def finish(p: subprocess.Popen) -> tuple[int, dict | None]:
    out, err = p.communicate(timeout=10)
    print(err)  # shown by pytest on failure
    return p.returncode, json.loads(out) if out else None


def until(url: str, key: str, check, timeout: float = 5) -> dict:
    client, deadline = Client(url, 0, 0), time.monotonic() + timeout
    while True:
        try:
            job = client.once("GET", f"/lookup/{key}")
            if check(job):
                return job
        except OSError:
            pass  # 404 until the submit lands
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_done_writes_outputs_releases_and_a_rerun_runs_nothing(tmp_path):
    with serve(tmp_path) as (url, _):
        rc, job = finish(labctl(url, out=tmp_path / "w1"))
        assert rc == 0 and job["state"] == "done" and job["released_at"]
        rc, again = finish(labctl(url, out=tmp_path / "w2"))  # same default key: the stored job, nothing new
        assert rc == 0 and again["job_id"] == job["job_id"] and again["attempt"] == 1

    for w in ("w1", "w2"):  # a fresh work dir gets the same stored outputs
        assert json.loads((tmp_path / w / "result.json").read_text()) == job["result"]
        prov = json.loads((tmp_path / w / "provenance.json").read_text())
        assert prov["job_id"] == job["job_id"] and prov["state"] == "done" and "result" not in prov
        assert (prov["device_id"], prov["protocol_version"], prov["sample_ids"]) == ("fake-1", "1.0", ["S1", "S2"])
    log = (tmp_path / "w1" / "labctl.log").read_text()
    assert "-> POST /jobs {" in log and "<- 201 {" in log and f"-> POST /jobs/{job['job_id']}/release" in log
    assert "<- 200 {" in (tmp_path / "w2" / "labctl.log").read_text()


def test_exit_codes_follow_the_stop_boundary(tmp_path):
    with serve(tmp_path) as (url, _):
        assert finish(labctl(url, "preflight_fail"))[0] == 75
        assert finish(labctl(url, "protocol_fail", out=tmp_path / "w"))[0] == 1
        assert json.loads((tmp_path / "w" / "provenance.json").read_text())["state"] == "failed_in_protocol"
        assert not (tmp_path / "w" / "result.json").exists()  # no result: the task fails without outputs
        assert finish(labctl(url, key="other", wait=False))[0] == 0  # submitted, not waited on
        assert finish(labctl(url, "estop", key="other"))[0] == 1  # 409: key used with another payload


def test_term_safe_stops_before_the_boundary(tmp_path):
    with serve(tmp_path, step=1) as (url, _):
        p = labctl(url, key="k", out=tmp_path / "w")
        until(url, "k", lambda j: j["state"] == "preflight")
        p.send_signal(signal.SIGTERM)
        assert finish(p)[0] == 143
        assert "/safe_stop" in (tmp_path / "w" / "labctl.log").read_text()
        job = until(url, "k", lambda j: j["state"] == "failed_preflight")
        assert job["reason"] == "safe_stop"


def test_unreachable_adapter_exits_69_and_the_retry_asks_the_ledger(tmp_path):
    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    assert finish(labctl(dead, grace=0.3, out=tmp_path / "w"))[0] == 69  # never submitted
    assert "<- error:" in (tmp_path / "w" / "labctl.log").read_text()

    with serve(tmp_path) as (url, stop):
        p = labctl(url, "silent", key="k", grace=0.3)  # 'silent' hangs past the boundary
        while "stoppable=False" not in p.stderr.readline():
            pass
        stop()
        assert finish(p)[0] == 69  # labctl can't know what happened meanwhile
    with serve(tmp_path) as (url, _):  # same ledger: the restart failed the job in protocol
        assert finish(labctl(url, "silent", key="k"))[0] == 1  # the retry gets the truth
