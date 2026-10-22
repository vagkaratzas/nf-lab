"""SQLite job ledger: every idempotency key the adapter accepted, and its outcome.

At-most-once: the partial UNIQUE index allows at most one job per key that is
not a pre-flight failure or voided. A key can only get a new attempt while every
earlier attempt failed before any liquid moved, or a human voided it.

Device lock: derived from the ledger, so it survives restarts. A device is
locked while one of its jobs has started and is not yet released.

Decks: the plate a person loaded on each device, also kept here. A device runs
a queued job only when it is free and holds the job's plate (free is not ready).

Device pool: a job names a capability, not a device. It stays unbound
(device_id NULL) while queued; the free device of that capability whose deck
holds the job's plate takes it. A plate is on one deck at most, so one device
can take it. A job may also name a queue: then only that queue's devices of the
capability take it. Which devices may take a job is the adapter's call (`takes`).
"""

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from .contract import FINAL, Job, SubmitRequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    device_id TEXT,  -- NULL while queued: bound when a device of the pool takes the job
    capability TEXT NOT NULL,
    queue TEXT,  -- NULL: any device of the capability
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
    ON jobs(idempotency_key) WHERE state NOT IN ('failed_preflight', 'voided');
CREATE TABLE IF NOT EXISTS decks (
    device_id TEXT PRIMARY KEY,
    plate TEXT UNIQUE,  -- NULL: empty deck. UNIQUE: a plate is on one device at most
    loaded_at TEXT,
    loaded_by TEXT
);
PRAGMA user_version = 4;
"""
VERSION = 4  # bump with any SCHEMA change

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
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'jobs'").fetchone():
            self.db.executescript(SCHEMA)
        elif (v := self.db.execute("PRAGMA user_version").fetchone()[0]) != VERSION:
            # shortcut: no migrations while the contract is a prototype; add them once ledgers hold real runs.
            raise RuntimeError(f"{path} is ledger version {v}, this adapter needs {VERSION}:"
                               " delete it or point NF_LAB_LEDGER elsewhere")
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
        """The live job for a key, else its latest attempt (failed pre-flight or voided)."""
        return self._job(
            self.db.execute(
                """SELECT * FROM jobs WHERE idempotency_key = ?
                   ORDER BY state NOT IN ('failed_preflight', 'voided') DESC, attempt DESC LIMIT 1""",
                (key,),
            ).fetchone()
        )

    def submit(self, req: SubmitRequest) -> tuple[Job, bool]:
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
                    """INSERT INTO jobs (job_id, idempotency_key, attempt, capability, queue, protocol,
                                         protocol_version, plate, wells, state, submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                    (job_id, req.idempotency_key, attempts + 1, req.capability, req.queue, req.protocol,
                     req.protocol_version, req.plate, json.dumps([w.model_dump(mode="json") for w in req.wells]), now()),
                )
            except sqlite3.IntegrityError:  # a live job already holds this key
                return earlier, False
        return self.get(job_id), True

    def held(self, device_id: str) -> Job | None:
        """The job locking the device: started and not released.

        A pre-flight failure never locks: nothing was touched and no outputs wait
        on the deck, so its retry (same key) and other jobs can start without a release.
        """
        return self._job(self.db.execute(
            "SELECT * FROM jobs WHERE device_id = ? AND started_at IS NOT NULL AND released_at IS NULL"
            " AND state != 'failed_preflight'",
            (device_id,),
        ).fetchone())

    def queued(self) -> list[Job]:
        """Every queued job, oldest first."""
        rows = self.db.execute("SELECT * FROM jobs WHERE state = 'queued' ORDER BY rowid")
        return [self._job(r) for r in rows]

    def last(self, device_id: str) -> Job | None:
        """The job the device started most recently."""
        return self._job(self.db.execute(
            "SELECT * FROM jobs WHERE device_id = ? ORDER BY started_at DESC LIMIT 1", (device_id,)).fetchone())

    def next_runnable(self, device_id: str, takes: Callable[[Job], bool]) -> Job | None:
        """Oldest job the device takes for the plate on its deck; None while the device is locked."""
        if self.held(device_id):
            return None
        plate = self.plate(device_id)
        return next((j for j in self.queued() if j.plate == plate and takes(j)), None)

    def plate(self, device_id: str) -> str | None:
        row = self.db.execute("SELECT plate FROM decks WHERE device_id = ?", (device_id,)).fetchone()
        return row["plate"] if row else None

    def where(self, plate: str) -> str | None:
        """The device holding the plate, if any."""
        row = self.db.execute("SELECT device_id FROM decks WHERE plate = ?", (plate,)).fetchone()
        return row["device_id"] if row else None

    def set_plate(self, device_id: str, plate: str | None, by: str) -> None:
        """Record what a person put on the deck; None: they emptied it."""
        self.db.execute(
            """INSERT INTO decks (device_id, plate, loaded_at, loaded_by) VALUES (?, ?, ?, ?)
               ON CONFLICT (device_id) DO UPDATE SET plate = excluded.plate,
                   loaded_at = excluded.loaded_at, loaded_by = excluded.loaded_by""",
            (device_id, plate, now(), by),
        )

    def unload(self, device_id: str, by: str) -> None:
        """The plate is off the deck: release the job holding the device, if any, and empty the deck."""
        with self.db:  # one transaction: a released job never leaves its plate recorded on the deck
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE jobs SET released_at = ? WHERE device_id = ? AND started_at IS NOT NULL"
                " AND released_at IS NULL AND state != 'failed_preflight'",
                (now(), device_id),
            )
            self.set_plate(device_id, None, by)

    def update(self, job_id: str, **fields) -> Job:
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"])
        if fields.get("state") in FINAL:
            fields.setdefault("finished_at", now())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", (*fields.values(), job_id))
        return self.get(job_id)
