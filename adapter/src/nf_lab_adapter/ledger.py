"""SQLite job ledger: every idempotency key the adapter accepted, and its outcome.

At-most-once: the partial UNIQUE index allows at most one job per key that is
not a pre-flight failure. A key can only get a new attempt while every earlier
attempt failed before any liquid moved.

Device lock: derived from the ledger, so it survives restarts. A device is
locked while one of its jobs has started and is not yet released.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from .contract import FINAL, Job, State, SubmitRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    protocol TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    plate TEXT NOT NULL,
    wells TEXT NOT NULL,
    state TEXT NOT NULL,
    stoppable INTEGER NOT NULL DEFAULT 1,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    result TEXT,
    submitted_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    released_at TEXT,
    device_heartbeat_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_job_per_key
    ON jobs(idempotency_key) WHERE state != 'failed_preflight';
"""

PAYLOAD = ("capability", "protocol", "protocol_version", "plate", "wells")


class KeyConflict(Exception):
    """The key is already in the ledger with a different payload."""


def now() -> str:
    return datetime.now(UTC).isoformat()


class Ledger:
    def __init__(self, path: str):
        # one connection, used only from the event loop thread; a pool if that changes.
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if "wells" not in {c["name"] for c in self.db.execute("PRAGMA table_info(jobs)")}:
            # shortcut: no migrations while the contract is a prototype; add them once ledgers hold real runs.
            raise RuntimeError(f"{path} predates the plate map (sample_ids column): delete it or point NF_LAB_LEDGER elsewhere")
        self._recover()

    def _recover(self) -> None:
        """Jobs in flight when the adapter died can't be resumed in-process.

        Still stoppable: nothing consumed, so it's a pre-flight failure (retryable).
        Past the stop boundary: material was touched, so never re-run.
        """
        with self.db:
            self.db.execute(
                """UPDATE jobs SET reason = 'adapter_restarted', finished_at = ?,
                       state = CASE WHEN stoppable THEN 'failed_preflight' ELSE 'failed_in_protocol' END
                   WHERE state IN ('preflight', 'running')""",
                (now(),),
            )

    def _job(self, row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        d = dict(row)
        d["wells"] = json.loads(d["wells"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return Job(**d)

    def get(self, job_id: str) -> Job | None:
        return self._job(self.db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone())

    def lookup(self, key: str) -> Job | None:
        """The live job for a key, else its latest pre-flight failure."""
        return self._job(
            self.db.execute(
                """SELECT * FROM jobs WHERE idempotency_key = ?
                   ORDER BY state != 'failed_preflight' DESC, attempt DESC LIMIT 1""",
                (key,),
            ).fetchone()
        )

    def submit(self, req: SubmitRequest, device_id: str) -> tuple[Job, bool]:
        """Insert a job unless the key already has a live one. Returns (job, created)."""
        with self.db:  # one transaction: the count, the check and the insert
            self.db.execute("BEGIN IMMEDIATE")
            earlier = self.lookup(req.idempotency_key)
            if earlier and earlier.model_dump(include=set(PAYLOAD)) != req.model_dump(include=set(PAYLOAD)):
                raise KeyConflict(req.idempotency_key)
            attempts = self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE idempotency_key = ?", (req.idempotency_key,)
            ).fetchone()[0]
            job_id = uuid.uuid4().hex
            try:
                self.db.execute(
                    """INSERT INTO jobs (job_id, idempotency_key, attempt, device_id, capability, protocol,
                                         protocol_version, plate, wells, state, submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                    (job_id, req.idempotency_key, attempts + 1, device_id, req.capability, req.protocol,
                     req.protocol_version, req.plate, json.dumps([w.model_dump(mode="json") for w in req.wells]), now()),
                )
            except sqlite3.IntegrityError:  # a live job already holds this key
                return earlier, False
        return self.get(job_id), True

    def next_runnable(self, device_id: str) -> Job | None:
        """Oldest queued job for the device, or None while the device is locked.

        Locked = a job has started and isn't released. A pre-flight failure never
        locks: nothing was touched and no outputs wait on the deck, so its retry
        (same key) and other jobs can start without a release.
        """
        locked = self.db.execute(
            "SELECT 1 FROM jobs WHERE device_id = ? AND started_at IS NOT NULL AND released_at IS NULL"
            " AND state != 'failed_preflight'",
            (device_id,),
        ).fetchone()
        if locked:
            return None
        return self._job(
            self.db.execute(
                "SELECT * FROM jobs WHERE device_id = ? AND state = 'queued' ORDER BY rowid LIMIT 1",
                (device_id,),
            ).fetchone()
        )

    def update(self, job_id: str, **fields) -> Job:
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"])
        if fields.get("state") in FINAL:
            fields.setdefault("finished_at", now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", (*fields.values(), job_id))
        return self.get(job_id)
