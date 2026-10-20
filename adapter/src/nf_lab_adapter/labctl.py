"""labctl: the adapter's CLI client, run as a wet task's script (executor shape B).

`labctl run --wait` submits the job (for a key already in the ledger the adapter
returns the existing job, so this is also the lookup), polls its status, releases
the device when it is done, prints the final job as JSON and exits with:

    0    done
    75   failed before any liquid moved: retryable (errorStrategy retries only 75)
    1    failed in protocol, stopped by a human, or a request was refused
    69   adapter unreachable for longer than `--grace` seconds: outcome unknown
    143  TERM (run abort) or 130 INT: safe_stop sent; the ledger keeps the outcome

69 is retried like 75 but means something else: labctl can't tell whether liquid
moved while the adapter was away. The retry resubmits the same key and the ledger
answers: the live job if one exists, a new attempt only if every earlier one
failed pre-flight. So nothing runs twice.

Stdlib only, so it runs wherever the task runs.
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

EXIT = {"done": 0, "failed_preflight": 75, "failed_in_protocol": 1, "stopped": 1}


class Unreachable(Exception):
    """The adapter didn't answer for the whole grace period."""


def log(msg: str) -> None:
    print(f"labctl: {msg}", file=sys.stderr, flush=True)


def default_key(protocol: str, version: str, samples: list[str]) -> str:
    # Sample order is kept: it decides which well each sample goes to.
    return hashlib.sha256(json.dumps([protocol, version, samples]).encode()).hexdigest()


class Client:
    def __init__(self, url: str, grace: float, poll: float):
        self.url, self.grace, self.poll = url.rstrip("/"), grace, poll

    def once(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data, {"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

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


def run(args: argparse.Namespace) -> int:
    key = args.key or default_key(args.protocol, args.protocol_version, args.samples)
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
            "protocol_version": args.protocol_version, "sample_ids": args.samples,
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
        if job["state"] == "done":
            job = client.call("POST", f"/jobs/{job['job_id']}/release")
        else:
            log(f"{job['state']}: {job['reason']}")
        print(json.dumps(job))
        return EXIT[job["state"]]
    except Unreachable as e:
        log(f"giving up: {e}; outcome unknown, a retry asks the ledger")
        return 69
    except urllib.error.HTTPError as e:
        log(f"refused: {e.code} {e.read().decode()}")
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="labctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="submit (or find) a job and wait for it")
    r.add_argument("--capability", required=True)
    r.add_argument("--protocol", required=True)
    r.add_argument("--protocol-version", required=True)
    r.add_argument("--samples", nargs="+", required=True, metavar="ID")
    r.add_argument("--key", help="idempotency key (default: sha256 of protocol, version and samples)")
    r.add_argument("--wait", action="store_true", help="poll until the job ends; exit with its code")
    r.add_argument("--adapter", default=os.environ.get("NF_LAB_ADAPTER", "http://127.0.0.1:8000"))
    r.add_argument("--poll", type=float, default=2.0, help="seconds between status polls")
    r.add_argument("--grace", type=float, default=300.0, help="seconds to absorb an unreachable adapter")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
