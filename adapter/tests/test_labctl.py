import json
import os
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
from nf_lab_adapter.bindings.fake import FakeBackend
from nf_lab_adapter.labctl import REPORT_COLUMNS, Client, default_key, task_hash


@contextmanager
def serve(tmp_path, step: float = 0.05, token: str | None = None):
    """The adapter on a real port, since labctl talks HTTP. Yields (url, stop)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(str(tmp_path / "ledger.sqlite"),
                     [Device("fake-1", "liquid-handling", FakeBackend(step), simulated_operator=True)], token=token)
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


def labctl(url: str, protocol: str = "ok", key: str | None = None, grace: float | None = 2, wait=True,
           out: Path | None = None, wells=("A1:S1", "B1:S2"), poll: float = 0.05, session: str | None = None,
           params: str | None = None, **popen):
    cmd = [sys.executable, "-m", "nf_lab_adapter.labctl", "run", "--capability", "liquid-handling",
           "--protocol", protocol, "--protocol-version", "1.0", "--plate", "P1", "--wells", *wells,
           "--adapter", url, "--poll", str(poll), "--outdir", str(out or tempfile.mkdtemp())]
    cmd += ["--grace", str(grace)] if grace is not None else []
    cmd += ["--key", key] if key else []
    cmd += ["--wait"] if wait else []
    cmd += ["--session", session] if session else []
    cmd += ["--params", params] if params else []
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


def test_done_writes_outputs_and_a_rerun_runs_nothing(tmp_path):
    with serve(tmp_path) as (url, _):
        rc, job = finish(labctl(url, out=tmp_path / "w1"))
        assert rc == 0 and job["state"] == "done" and job["released_at"]  # by the simulated operator
        rc, again = finish(labctl(url, out=tmp_path / "w2", wells=("B1:S2", "A1:S1")))  # same key, any order
        assert rc == 0 and again["job_id"] == job["job_id"] and again["attempt"] == 1

    for w in ("w1", "w2"):  # a fresh work dir gets the same stored outputs
        assert json.loads((tmp_path / w / "result.json").read_text()) == job["result"]
        prov = json.loads((tmp_path / w / "provenance.json").read_text())
        assert prov["job_id"] == job["job_id"] and prov["state"] == "done" and "result" not in prov
        assert (prov["device_id"], prov["protocol_version"], prov["plate"]) == ("fake-1", "1.0", "P1")
        assert [(w["row"], w["column"], w["sample"], w["type"]) for w in prov["wells"]] == [
            ("A", 1, "S1", "sample"), ("B", 1, "S2", "sample")]
    log = (tmp_path / "w1" / "labctl.log").read_text()
    assert "-> POST /jobs {" in log and "<- 201 {" in log and "/release" not in log  # unloading is a person's job
    assert "<- 200 {" in (tmp_path / "w2" / "labctl.log").read_text()


def test_a_malformed_well_is_refused_before_any_request(tmp_path):
    p = labctl("http://127.0.0.1:9", wells=("A1",), out=tmp_path / "w")
    assert finish(p)[0] == 2 and not (tmp_path / "w").exists()  # argparse error


def test_exit_codes_follow_the_stop_boundary(tmp_path):
    with serve(tmp_path) as (url, _):
        assert finish(labctl(url, "preflight_fail"))[0] == 75
        assert finish(labctl(url, "protocol_fail", out=tmp_path / "w"))[0] == 1
        assert json.loads((tmp_path / "w" / "provenance.json").read_text())["state"] == "failed_in_protocol"
        assert not (tmp_path / "w" / "result.json").exists()  # no result: the task fails without outputs
        assert finish(labctl(url, key="other", wait=False))[0] == 0  # submitted, not waited on
        assert finish(labctl(url, "estop", key="other"))[0] == 1  # 409: key used with another payload


def test_a_job_voided_between_polls_ends_labctl(tmp_path):
    """The person presses Failed before labctl's next poll: labctl sees `voided`, not `stopped`."""
    with serve(tmp_path) as (url, _):
        p = labctl(url, "estop", key="v", poll=1.5)
        job = until(url, "v", lambda j: j["state"] == "stopped")
        Client(url, 0, 0).once("POST", f"/jobs/{job['job_id']}/resolve", {"outcome": "void"})
        code, job = finish(p)
        assert (code, job["state"]) == (1, "voided")


def test_term_safe_stops_before_the_boundary(tmp_path):
    with serve(tmp_path, step=1) as (url, _):
        p = labctl(url, key="k", out=tmp_path / "w")
        until(url, "k", lambda j: j["state"] == "preflight")
        p.send_signal(signal.SIGTERM)
        assert finish(p)[0] == 143
        assert "/safe_stop" in (tmp_path / "w" / "labctl.log").read_text()
        job = until(url, "k", lambda j: j["state"] == "failed_preflight")
        assert job["reason"] == "safe_stop"


def test_term_safe_stops_when_stderr_is_gone(tmp_path):
    # On abort the wrapper TERMs the whole task tree, `tee .command.err` included.
    with serve(tmp_path, step=1) as (url, _):
        p = labctl(url, key="k", out=tmp_path / "w")
        p.stderr.close()  # no reader: every write to stderr now fails (EPIPE)
        until(url, "k", lambda j: j["state"] == "preflight")
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 143
        assert "/safe_stop" in (tmp_path / "w" / "labctl.log").read_text()
        assert until(url, "k", lambda j: j["state"] == "failed_preflight")["reason"] == "safe_stop"


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


def test_grace_defaults_to_the_environment(tmp_path):
    # the lab executor sets NF_LAB_GRACE from `lab.grace`; without it labctl would wait 300 s here
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    p = labctl(dead, grace=None, out=tmp_path, env={**os.environ, "NF_LAB_GRACE": "0.3"})
    assert p.wait(timeout=30) == 69


def test_task_hash_is_read_from_a_nextflow_work_dir():
    assert task_hash(Path("/runs/work/ab") / ("c" * 30)) == "ab" + "c" * 30
    assert task_hash(Path("/tmp/somewhere")) is None


def test_report_lists_a_sessions_attempts_with_their_lineage_ids(tmp_path):
    first, retry = tmp_path / "work" / "ab" / ("c" * 30), tmp_path / "work" / "de" / ("f" * 30)
    with serve(tmp_path) as (url, _):
        assert finish(labctl(url, "preflight_once", out=first, session="s1"))[0] == 75
        assert finish(labctl(url, "preflight_once", out=retry, session="s1"))[0] == 0
        report = subprocess.run([sys.executable, "-m", "nf_lab_adapter.labctl", "report", "--session", "s1",
                                 "--adapter", url, "--outdir", str(tmp_path / "r")], capture_output=True, text=True)
        assert report.returncode == 0, report.stderr
    jobs = json.loads((tmp_path / "r" / "lab_report.json").read_text())["jobs"]
    assert [(j["attempt"], j["state"]) for j in jobs] == [(1, "failed_preflight"), (2, "done")]
    assert "result" not in jobs[1]
    header, *rows = (tmp_path / "r" / "lab_report.tsv").read_text().splitlines()
    assert header.split("\t") == list(REPORT_COLUMNS)
    assert [r.split("\t")[-1] for r in rows] == [f"lid://ab{'c' * 30}", f"lid://de{'f' * 30}"]


def test_labctl_sends_the_token_from_the_environment(tmp_path):
    with serve(tmp_path, token="t0ken") as (url, _):
        env = {k: v for k, v in os.environ.items() if k != "NF_LAB_TOKEN"}
        out = tmp_path / "none"
        code, _ = finish(labctl(url, key="t1", out=out, env=env))
        assert code == 1 and "refused: 401" in (out / "labctl.log").read_text()  # not retried: 69/75 only
        code, job = finish(labctl(url, key="t1", env={**env, "NF_LAB_TOKEN": "t0ken"}))
        assert code == 0 and job["state"] == "done"
        assert "t0ken" not in (out / "labctl.log").read_text()  # the log never holds the token



def test_params_reach_the_job_through_stdin_and_join_the_key(tmp_path):
    with serve(tmp_path) as (url, _):
        p = labctl(url, "plate_transfer", out=tmp_path / "a", params="-", stdin=subprocess.PIPE)
        out, err = p.communicate('{"volume_ul": 120}\n', timeout=10)  # a wet module's heredoc
        assert p.returncode == 0, err
        provenance = json.loads((tmp_path / "a" / "provenance.json").read_text())
        assert provenance["params"] == {"volume_ul": 120} and provenance["program"] == {"volume_ul": 120.0}
        wells = [{"row": "A", "column": 1, "sample": "S1", "type": "sample"},
                 {"row": "B", "column": 1, "sample": "S2", "type": "sample"}]
        assert provenance["idempotency_key"] == default_key("plate_transfer", "1.0", "P1", wells, {"volume_ul": 120})

        code, job = finish(labctl(url, "plate_transfer", params="null"))  # a module with no ext.params
        assert code == 0 and job["params"] == {} and job["program"] == {"volume_ul": 150.0}


def test_the_key_without_params_is_the_key_before_params():
    wells = [{"row": "A", "column": 1, "sample": "S1", "type": "sample"}]
    before = __import__("hashlib").sha256(json.dumps(["p", "1.0", "P1", wells]).encode()).hexdigest()
    assert default_key("p", "1.0", "P1", wells) == default_key("p", "1.0", "P1", wells, {}) == before
    assert default_key("p", "1.0", "P1", wells, {"a": 1, "b": 2}) == default_key("p", "1.0", "P1", wells, {"b": 2, "a": 1})
    assert default_key("p", "1.0", "P1", wells, {"a": 1}) not in (before, default_key("p", "1.0", "P1", wells, {"a": 2}))


def test_a_param_the_adapter_refuses_fails_the_task_and_queues_nothing(tmp_path):
    with serve(tmp_path) as (url, _):
        code, _ = finish(labctl(url, "plate_transfer", key="bad", out=tmp_path / "o", params='{"volume_ul": 900}'))
        assert code == 1  # not retried: the same params would be refused again
        assert "refused: 422" in (tmp_path / "o" / "labctl.log").read_text()
        assert not (tmp_path / "o" / "provenance.json").exists()
        assert Client(url, 0, 0).call("GET", "/devices")[0]["status"] != "running"


def test_params_that_are_not_an_object_of_scalars_are_refused_before_any_request(tmp_path):
    for i, bad in enumerate(['"a string"', "[1]", '{"g": [1]}', '{"g": NaN}', '{"g": 1e400}', "{nope"]):
        out = tmp_path / str(i)
        code, _ = finish(labctl("http://127.0.0.1:9", "plate_spin", out=out, params=bad))
        log = (out / "labctl.log").read_text()
        assert code == 1 and "refused: --params" in log and "-> POST" not in log, (bad, log)


def bare_labctl(url: str, out: Path, *extra: str, env: dict, key: str = "env") -> subprocess.Popen:
    """labctl as a wet module runs it under the lab executor: no --capability, --queue or --session."""
    cmd = [sys.executable, "-m", "nf_lab_adapter.labctl", "run", "--wait", "--protocol", "ok", "--protocol-version", "1.0",
           "--plate", "P1", "--wells", "A1:S1", "--adapter", url, "--poll", "0.05", "--outdir", str(out), "--key", key, *extra]
    clean = {k: v for k, v in os.environ.items() if not k.startswith("NF_LAB_")}
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**clean, **env})


def test_capability_queue_and_session_come_from_the_lab_executors_env(tmp_path):
    with serve(tmp_path) as (url, _):
        workdir = tmp_path / "ab" / ("c" * 30)  # a Nextflow work dir: the ledger records the task with the session
        code, job = finish(bare_labctl(url, workdir, env={"NF_LAB_CAPABILITY": "liquid-handling", "NF_LAB_SESSION": "s1"}))
        assert code == 0 and (job["capability"], job["queue"]) == ("liquid-handling", None)
        assert [j["job_id"] for j in Client(url, 0, 0).call("GET", "/jobs?session=s1")] == [job["job_id"]]

        # a flag wins over the environment: no robot is in queue "elsewhere", so the submit is refused
        code, _ = finish(bare_labctl(url, tmp_path / "b", "--queue", "elsewhere", key="flag",
                                     env={"NF_LAB_CAPABILITY": "liquid-handling", "NF_LAB_QUEUE": ""}))
        assert code == 1 and "refused: 404" in (tmp_path / "b" / "labctl.log").read_text()


def test_no_capability_anywhere_is_a_usage_error(tmp_path):
    p = bare_labctl("http://127.0.0.1:9", tmp_path / "o", env={})
    _, err = p.communicate(timeout=10)
    assert p.returncode == 2 and "no capability" in err
