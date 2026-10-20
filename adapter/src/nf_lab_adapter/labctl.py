#!/usr/bin/env python3
"""labctl: the adapter's CLI client, run as a wet task's script (executor shape B).

`labctl run --wait` submits the job (for a key already in the ledger the adapter
returns the existing job, so this is also the lookup), polls its status, prints
the final job as JSON and exits with:

    0    done
    75   failed before any liquid moved: retryable (errorStrategy retries 75 and 69)
    1    failed in protocol, stopped by a human, or a request was refused
    69   adapter unreachable for longer than `--grace` seconds: outcome unknown
    143  TERM (run abort) or 130 INT: safe_stop sent; the ledger keeps the outcome

69 is retried like 75 but means something else: labctl can't tell whether liquid
moved while the adapter was away. The retry resubmits the same key and the ledger
answers: the live job if one exists, a new attempt only if every earlier one
failed pre-flight. So nothing runs twice.

Files in `--outdir` (default: the current dir, i.e. the task work dir):

    labctl.log       always: every request and reply, timestamped
    provenance.json  any final state: the job as the ledger has it, minus its result
    result.json      done only: the job's result

They come from the ledger: a key that is already done writes its stored outputs
and runs nothing. labctl never releases the device: the person who unloads the
plate does (or the adapter's simulated operator).

Stdlib only (Python >= 3.10), so it runs wherever the task runs.
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXIT = {"done": 0, "failed_preflight": 75, "failed_in_protocol": 1, "stopped": 1}


class Unreachable(Exception):
    """The adapter didn't answer for the whole grace period."""


_logfile = None  # labctl.log, opened by run()


def log(msg: str, quiet: bool = False) -> None:
    """To labctl.log and stderr; quiet = labctl.log only."""
    if _logfile:
        _logfile.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
        _logfile.flush()
    if not quiet:
        try:
            print(f"labctl: {msg}", file=sys.stderr, flush=True)
        except OSError:
            # stderr's reader is gone: on abort the wrapper TERMs its `tee .command.err` too.
            # Carry on without stderr, or the safe_stop never goes out.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())


def well(token: str) -> dict:
    """`A1:S01` or `D2:NC1:negative_control` (type defaults to sample)."""
    parts = token.split(":")
    if len(parts) not in (2, 3) or len(parts[0]) < 2 or not parts[0][1:].isdigit() or not parts[1]:
        raise argparse.ArgumentTypeError(f"{token!r}: expected WELL:SAMPLE[:TYPE], e.g. A1:S01")
    return {"row": parts[0][0].upper(), "column": int(parts[0][1:]), "sample": parts[1],
            "type": parts[2] if len(parts) == 3 else "sample"}


def default_key(protocol: str, version: str, plate: str, wells: list[dict]) -> str:
    # wells sorted by the caller (row, column): the order they're listed in carries no meaning
    return hashlib.sha256(json.dumps([protocol, version, plate, wells]).encode()).hexdigest()


class Client:
    def __init__(self, url: str, grace: float, poll: float):
        self.url, self.grace, self.poll = url.rstrip("/"), grace, poll

    def once(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
        # shortcut: every poll reply is logged in full (~700 B), dedupe unchanged replies if multi-day runs make it big.
        data = json.dumps(body).encode() if body is not None else None
        log(f"-> {method} {path}" + (f" {data.decode()}" if data else ""), quiet=True)
        req = urllib.request.Request(self.url + path, data, {"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                reply = r.read().decode()
                log(f"<- {r.status} {reply}", quiet=True)
                return json.loads(reply)
        except urllib.error.HTTPError as e:
            e.body = e.read().decode()  # readable once; kept for the caller
            log(f"<- {e.code} {e.body}", quiet=True)
            raise
        except OSError as e:
            log(f"<- error: {e}", quiet=True)
            raise

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        """Retry connection errors and 5xx for up to `grace` seconds; 4xx raise at once."""
        deadline = time.monotonic() + self.grace
        while True:
            try:
                return self.once(method, path, body)
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise
                err = e
            except OSError as e:  # URLError, refused, reset, timeout
                err = e
            if time.monotonic() >= deadline:
                raise Unreachable(f"{method} {path}: {err}")
            log(f"adapter unreachable ({err}); retrying")
            time.sleep(self.poll)


def write_outputs(outdir: Path, job: dict) -> None:
    (outdir / "provenance.json").write_text(json.dumps({k: v for k, v in job.items() if k != "result"}, indent=2))
    if job["state"] == "done":
        (outdir / "result.json").write_text(json.dumps(job["result"], indent=2))


def run(args: argparse.Namespace) -> int:
    global _logfile
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    _logfile = open(outdir / "labctl.log", "a")
    wells = sorted(args.wells, key=lambda w: (w["row"], w["column"]))
    key = args.key or default_key(args.protocol, args.protocol_version, args.plate, wells)
    client = Client(args.adapter, args.grace, args.poll)
    job: dict | None = None

    def on_signal(signum, _frame):
        # Look the key up rather than trusting `job`: the submit may have landed
        # without its reply. One try each, no grace: the wrapper is exiting.
        log(f"signal {signum}: safe_stop")
        try:
            jid = client.once("GET", f"/lookup/{key}", timeout=5)["job_id"]
            log(json.dumps(client.once("POST", f"/jobs/{jid}/safe_stop", timeout=5)))
        except Exception as e:
            log(f"safe_stop failed: {e}")
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        job = client.call("POST", "/jobs", {
            "capability": args.capability, "protocol": args.protocol,
            "protocol_version": args.protocol_version, "plate": args.plate, "wells": wells,
            "idempotency_key": key,
        })
        log(f"job {job['job_id']} attempt {job['attempt']} on {job['device_id']}: {job['state']}")
        if not args.wait:
            print(json.dumps(job))
            return 0
        seen = (job["state"], job["stoppable"])
        while job["state"] not in EXIT:
            time.sleep(args.poll)
            job = client.call("GET", f"/jobs/{job['job_id']}")
            if (job["state"], job["stoppable"]) != seen:
                seen = (job["state"], job["stoppable"])
                log(f"{job['state']}, stoppable={job['stoppable']}")
        write_outputs(outdir, job)
        if job["state"] != "done":
            log(f"{job['state']}: {job['reason']}")
        print(json.dumps(job))
        return EXIT[job["state"]]
    except Unreachable as e:
        log(f"giving up: {e}; outcome unknown, a retry asks the ledger")
        return 69
    except urllib.error.HTTPError as e:
        log(f"refused: {e.code} {e.body}")
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="labctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="submit (or find) a job and wait for it")
    r.add_argument("--capability", required=True)
    r.add_argument("--protocol", required=True)
    r.add_argument("--protocol-version", required=True)
    r.add_argument("--plate", required=True, help="plate ID from the plate map")
    r.add_argument("--wells", nargs="+", required=True, type=well, metavar="WELL:SAMPLE[:TYPE]",
                   help="the plate's filled wells, e.g. A1:S01 D2:NC1:negative_control H12:BLK1:blank")
    r.add_argument("--key", help="idempotency key (default: sha256 of protocol, version, plate and wells)")
    r.add_argument("--wait", action="store_true", help="poll until the job ends; exit with its code")
    r.add_argument("--adapter", default=os.environ.get("NF_LAB_ADAPTER", "http://127.0.0.1:8000"))
    r.add_argument("--poll", type=float, default=2.0, help="seconds between status polls")
    r.add_argument("--grace", type=float, default=300.0, help="seconds to absorb an unreachable adapter")
    r.add_argument("--outdir", default=".", help="where labctl.log, provenance.json and result.json go")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
