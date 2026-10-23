"""Device adapter: the device-job contract over HTTP, one worker per device.

Run:     uv run uvicorn nf_lab_adapter.app:main --factory
Export:  uv run python -m nf_lab_adapter.app > openapi.json

Two APIs:
- the device-job contract (submit, status, lookup, safe_stop, release, session_jobs), called by
  `labctl`. It's exported to openapi.json, and every adapter must serve it.
- the operator API (tag `operator`): what a person does at the deck, i.e. load
  a plate, unload it, e-stop, or resolve a failed job. `GET /deck` serves its
  buttons. A real lab may do these in the vendor UI instead, so the operator API
  isn't part of the contract.
"""

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .backends import Backend, DeviceStopped, FakeBackend
from .contract import FINAL, CollectedJob, Job, JobSpec, State, SubmitRequest
from .ledger import KeyConflict, Ledger, now

SIMULATED_OPERATOR = "simulated operator"
UNRESOLVED = {State.failed_in_protocol, State.stopped}  # a person resolves these before the unload


@dataclass
class Device:
    device_id: str
    capability: str
    backend: Backend
    # Seconds without a device report before the job fails as `device_silent`.
    # Per device: set it longer than the slowest step's reporting gap (incubators).
    silent_after: float = 600.0
    # Unload a done job's plate and load the plate the next queued job names, with
    # no person. For tests and unattended runs; failed jobs still wait for a person.
    simulated_operator: bool = False
    # Nextflow's `queue` directive picks it: a job naming a queue runs only on that queue's devices.
    queue: str | None = None

    def takes(self, job: JobSpec) -> bool:
        return (job.capability == self.capability and job.queue in (None, self.queue)
                and job.protocol in self.backend.PROTOCOLS)


class Conflict(Exception):
    """The request doesn't fit the device's or job's current state (HTTP 409)."""


class Operator(BaseModel):
    by: str = Field("operator", min_length=1, description="Who pressed the button")


class Load(Operator):
    plate: str = Field(min_length=1, description="Plate ID, as the plate map names it")


class Resolve(Operator):
    outcome: Literal["done", "void"] = Field(
        description="done: the deck shows the step finished, the result is read off the deck. "
                    "void: the material was thrown away, the key may run again")
    note: str = ""


class DeviceStatus(StrEnum):
    empty = "empty"  # nothing on the deck: load a plate
    ready = "ready"  # a plate on the deck, free: its queued jobs start here
    running = "running"
    unload = "unload"  # done or voided: a person takes the plate off
    needs_person = "needs_person"  # failed in protocol or stopped: check the deck, press Succeeded or Failed, unload


class DeviceView(BaseModel):
    device_id: str
    capability: str
    queue: str | None
    protocols: list[str]
    viewer: str | None = Field(description="Live view of the deck, if the backend serves one")
    simulated_operator: bool
    status: DeviceStatus
    detail: str = Field(description="The status in words, with the last job's pre-flight failure if any")
    plate: str | None = Field(description="Plate on the deck; None: empty")
    held: Job | None = Field(description="The job locking the device: running, or finished and not yet unloaded")


class QueuedJob(Job):
    waiting_for: str = Field(description="What has to happen at the decks before the job can start")


class Adapter:
    def __init__(self, ledger: Ledger, devices: list[Device]):
        self.ledger = ledger
        self.by_id = {d.device_id: d for d in devices}
        self.wake = {d.device_id: asyncio.Event() for d in devices}
        self.running: dict[str, asyncio.Task] = {}
        self.estops: dict[str, str] = {}  # job_id -> who pressed e-stop

    def wake_pool(self, capability: str) -> None:
        for d in self.by_id.values():
            if d.capability == capability:
                self.wake[d.device_id].set()

    async def worker(self, device: Device) -> None:
        """Run the pool's queued jobs one at a time, in submit order, each once its plate is on this deck."""
        wake = self.wake[device.device_id]
        while True:
            wake.clear()
            if device.simulated_operator:
                await self.operate(device)
            job = self.ledger.next_runnable(device.device_id, device.takes)
            if job is None:  # nothing queued for the plate on the deck, or the device is locked
                await wake.wait()
                continue
            await self.run_job(device, job)

    async def operate(self, device: Device) -> None:
        """The simulated operator's turn: unload a done or voided job, then load the oldest queued plate no deck holds."""
        held = self.ledger.held(device.device_id)
        if held and held.state in (State.done, State.voided):
            await self.unload(device, SIMULATED_OPERATOR)
        elif held:
            return
        queue = [j for j in self.ledger.queued() if device.takes(j)]
        if any(j.plate == self.ledger.plate(device.device_id) for j in queue):
            return  # the plate on the deck has work
        plate = next((j.plate for j in queue if self.ledger.where(j.plate) is None), None)
        if plate:
            try:
                await self.load(device, plate, SIMULATED_OPERATOR)
            except Conflict:  # another device loaded it meanwhile: look again
                self.wake[device.device_id].set()

    async def run_job(self, device: Device, job: Job) -> None:
        job = self.ledger.update(job.job_id, device_id=device.device_id, state=State.preflight, started_at=now(),
                                 device_heartbeat_at=now())

        def progress(*, state: State | None = None, stoppable: bool | None = None) -> None:
            fields = {"device_heartbeat_at": now()}
            if state is not None:
                fields["state"] = state
            if stoppable is not None:
                fields["stoppable"] = stoppable
            self.ledger.update(job.job_id, **fields)

        task = asyncio.create_task(device.backend.run(job, progress))
        self.running[job.job_id] = task
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=min(1.0, device.silent_after / 4))
                if not task.done() and self._silent(job.job_id, device):
                    # Stop driving a device we can no longer hear. Past the stop boundary the
                    # lock stays held: a human checks the deck before release.
                    task.cancel()
                    await asyncio.wait({task})
                    self._fail(job.job_id, "device_silent")
                    return
            self.ledger.update(job.job_id, state=State.done, result=task.result())
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # adapter shutting down: the ledger settles this job on restart
            if by := self.estops.pop(job.job_id, None):
                self.ledger.update(job.job_id, state=State.stopped, reason=f"e-stop pressed by {by}")
            else:
                self.ledger.update(job.job_id, state=State.failed_preflight, reason="safe_stop")
        except DeviceStopped as e:
            self.ledger.update(job.job_id, state=State.stopped, reason=str(e))
        except Exception as e:
            self._fail(job.job_id, f"{type(e).__name__}: {e}")
        finally:
            task.cancel()  # no-op once finished; stops the backend if the adapter shuts down
            self.running.pop(job.job_id, None)

    def _fail(self, job_id: str, reason: str) -> None:
        """The stop boundary alone decides whether a failure is retryable."""
        touched = not self.ledger.get(job_id).stoppable
        self.ledger.update(job_id, state=State.failed_in_protocol if touched else State.failed_preflight, reason=reason)

    def _silent(self, job_id: str, device: Device) -> bool:
        heartbeat = self.ledger.get(job_id).device_heartbeat_at
        return (datetime.now(UTC) - heartbeat).total_seconds() > device.silent_after

    def safe_stop(self, job: Job) -> Job:
        if job.state in FINAL:
            return job
        if job.state == State.queued:  # never started: nothing to stop, nothing consumed
            return self.ledger.update(job.job_id, state=State.failed_preflight, reason="safe_stop")
        if job.stoppable:
            # Safe: the backend is parked at an await before the boundary, so it
            # can't cross it before the cancellation lands. Poll status for the outcome.
            self.running[job.job_id].cancel()
            return job
        # past the boundary the job runs to completion; pause when a backend supports it.
        return self.ledger.update(job.job_id, stop_requested=True)

    # --- operator actions: what a person does at the deck ---

    async def load(self, device: Device, plate: str, by: str) -> None:
        """A person put the plate on the deck and checked it: the device is ready for that plate's jobs."""
        if held := self.ledger.held(device.device_id):
            raise Conflict(f"{device.device_id} holds plate {held.plate} (job {held.job_id}, {held.state}): unload it first")
        if (other := self.ledger.where(plate)) not in (None, device.device_id):
            raise Conflict(f"plate {plate} is on {other}")
        self.ledger.set_plate(device.device_id, None, by)  # nothing starts while the deck is being set up
        await device.backend.load(plate)
        try:
            self.ledger.set_plate(device.device_id, plate, by)
        except sqlite3.IntegrityError:  # loaded on another device meanwhile
            raise Conflict(f"plate {plate} is on {self.ledger.where(plate)}")
        self.wake[device.device_id].set()

    async def unload(self, device: Device, by: str) -> None:
        """A person took the plate off: this is the release of the job holding the device."""
        if (held := self.ledger.held(device.device_id)) and held.state not in FINAL:
            raise Conflict(f"job {held.job_id} is {held.state} on {device.device_id}: wait for it, or e-stop")
        if held and held.state in UNRESOLVED:  # unloaded unresolved, the job's key would stay stuck
            raise Conflict(f"job {held.job_id} is {held.state} on {device.device_id}: check the deck and resolve it"
                           " first (Succeeded or Failed), then unload")
        self.ledger.unload(device.device_id, by)
        await device.backend.load(None)
        for wake in self.wake.values():  # the plate may now go on another device, of any capability
            wake.set()

    def estop(self, device: Device, by: str) -> None:
        """Stop the running job now, past the stop boundary too: a person decided. The lock stays held."""
        held = self.ledger.held(device.device_id)
        if held is None or held.job_id not in self.running:
            raise Conflict(f"nothing running on {device.device_id}")
        self.estops[held.job_id] = by
        self.running[held.job_id].cancel()

    async def resolve(self, job: Job, r: Resolve) -> Job:
        """A person checked the deck after a failed job and records what really happened."""
        if job.state not in UNRESOLVED:
            raise Conflict(f"job {job.job_id} is {job.state}: only failed_in_protocol or stopped jobs are resolved")
        why = f"{r.by}{': ' + r.note if r.note else ''}; was {job.state}: {job.reason}"
        if r.outcome == "done":
            if job.released_at:
                raise Conflict(f"plate {job.plate} is already unloaded, so the deck can't be read: void the job instead")
            result = await self.by_id[job.device_id].backend.read_result(job)
            job = self.ledger.update(job.job_id, state=State.done, result=result, reason=f"resolved as done by {why}",
                                     finished_at=job.finished_at.isoformat())
        else:
            job = self.ledger.update(job.job_id, state=State.voided, reason=f"voided by {why}", finished_at=job.finished_at.isoformat())
        self.wake_pool(job.capability)
        return job

    def status(self, device: Device) -> tuple[DeviceStatus, str]:
        h, plate = self.ledger.held(device.device_id), self.ledger.plate(device.device_id)
        if h and h.state not in FINAL:
            return DeviceStatus.running, (f"running {h.protocol} on plate {h.plate}"
                                          f" ({h.state}{'' if h.stoppable else ', liquid moved'})")
        if h and h.state == State.done:
            return DeviceStatus.unload, f"done with plate {h.plate}: unload it"
        if h and h.state == State.voided:
            return DeviceStatus.unload, f"plate {h.plate} voided: unload it"
        if h:
            return DeviceStatus.needs_person, (f"{h.state} on plate {h.plate}: check the deck, press Succeeded or Failed,"
                                               f" then unload ({h.reason})")
        status, text = (DeviceStatus.ready, f"ready for plate {plate}") if plate else (DeviceStatus.empty, "empty deck: load a plate")
        if (last := self.ledger.last(device.device_id)) and last.state == State.failed_preflight:
            text += f"; last job (plate {last.plate}) failed pre-flight: {last.reason}"
        return status, text

    def view(self, device: Device) -> DeviceView:
        status, detail = self.status(device)
        return DeviceView(device_id=device.device_id, capability=device.capability, queue=device.queue,
                          protocols=sorted(device.backend.PROTOCOLS), viewer=device.backend.viewer,
                          simulated_operator=device.simulated_operator, status=status, detail=detail,
                          plate=self.ledger.plate(device.device_id), held=self.ledger.held(device.device_id))

    def waiting_for(self, job: Job) -> str:
        """What a person has to do at the decks before the queued job can start."""
        pool = [d for d in self.by_id.values() if d.takes(job)]
        if on := self.ledger.where(job.plate):
            device = self.by_id[on]
            if device not in pool:
                why = (f"has no {job.capability}" if device.capability != job.capability
                       else f"isn't in queue {job.queue}" if job.queue not in (None, device.queue)
                       else f"doesn't run {job.protocol}")
                return f"plate {job.plate} is on {on}, which {why}: unload it there"
            status, text = self.status(device)
            if status == DeviceStatus.ready:
                return f"plate {job.plate} is on {on}: starts there after earlier jobs for that plate"
            return f"plate {job.plate} is on {on}, {text}" + (f", then load {job.plate} again" if status != DeviceStatus.running else "")
        if free := [d.device_id for d in pool if not self.ledger.held(d.device_id)]:
            return f"plate {job.plate} isn't on any deck: load it on {' or '.join(free)}"
        busy = "; ".join(f"{d.device_id}: {self.status(d)[1]}" for d in pool)
        where = f" in queue {job.queue}" if job.queue else ""
        return f"plate {job.plate} isn't on any deck and no {job.capability} device{where} is free: {busy}"


bearer = HTTPBearer(auto_error=False, description="The adapter's token (NF_LAB_TOKEN); required when the adapter sets one")


def create_app(ledger_path: str, devices: list[Device], token: str | None = None) -> FastAPI:
    """token: every request must carry `Authorization: Bearer <token>`; None: no check (lab network only)."""
    ledger = Ledger(ledger_path)
    adapter = Adapter(ledger, devices)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for d in devices:
            await d.backend.setup()
            await d.backend.load(ledger.plate(d.device_id))  # a simulated deck starts as the ledger last saw it
        workers = [asyncio.create_task(adapter.worker(d)) for d in devices]
        yield
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def check_token(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        # One dependency on the whole app, so no route (or a later one) can skip the check.
        # The deck page's HTML holds nothing secret; its buttons' calls carry the token.
        if token is None or request.url.path == "/deck":
            return
        if cred is None or not secrets.compare_digest(cred.credentials.encode(), token.encode()):
            raise HTTPException(401, "missing or wrong bearer token: set NF_LAB_TOKEN to the adapter's token",
                                headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="nf-lab device adapter", version="0.1.0", lifespan=lifespan,
                  dependencies=[Depends(check_token)],
                  responses={401: {"description": "Missing or wrong bearer token (the adapter sets one)"}})

    # Request log for the dashboard: the contract calls and operator actions, one JSON line each.
    # shortcut: never rotated; fine for demo runs, rotate if the adapter runs for weeks.
    request_log = None if ledger_path == ":memory:" else Path(ledger_path).with_suffix(".requests.jsonl")

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if request_log and (request.method != "GET" or path.startswith(("/jobs/", "/lookup/"))):
            entry = {"t": now(), "method": request.method, "path": path, "status": response.status_code}
            if loc := response.headers.get("location"):
                entry["location"] = loc  # the job a submit created or found
            with request_log.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        return response

    @app.exception_handler(Conflict)
    async def conflict(_: Request, e: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=409)

    def get_job(job_id: str) -> Job:
        job = ledger.get(job_id)
        if job is None:
            raise HTTPException(404, f"unknown job {job_id}")
        return job

    def get_device(device_id: str) -> Device:
        if device_id not in adapter.by_id:
            raise HTTPException(404, f"unknown device {device_id}")
        return adapter.by_id[device_id]

    @app.post(
        "/jobs", operation_id="submit", response_model=Job, status_code=201,
        responses={
            200: {"model": Job, "description": "Key already in the ledger: the existing job, nothing new runs"},
            404: {"description": "No device offers this capability"},
            409: {"description": "Key already used with a different payload"},
        },
    )
    async def submit(req: SubmitRequest, response: Response) -> Job:
        """Queue a job for the capability's pool, or return the existing one for this idempotency key."""
        if not any(d.takes(req) for d in devices):
            where = f" in queue {req.queue!r}" if req.queue else ""
            raise HTTPException(404, f"no {req.capability!r} device{where} runs protocol {req.protocol!r}")
        try:
            job, created = ledger.submit(req)
        except KeyConflict:
            raise HTTPException(409, f"idempotency key {req.idempotency_key} already used with a different payload")
        if created:
            adapter.wake_pool(req.capability)
        else:
            response.status_code = 200
        response.headers["Location"] = f"/jobs/{job.job_id}"
        return job

    @app.get("/jobs", operation_id="session_jobs", response_model=list[CollectedJob])
    async def session_jobs(session: str) -> list[CollectedJob]:
        """Every job a Nextflow session submitted or collected, every attempt (failures too), with the tasks
        that collected it: each task hash is that task's lineage id. What `labctl report` writes."""
        return ledger.session(session)

    @app.get("/jobs/{job_id}", operation_id="status", response_model=Job, responses={404: {}})
    async def status(job_id: str) -> Job:
        """State, phase reached, stop boundary and device heartbeat."""
        return get_job(job_id)

    @app.get("/lookup/{idempotency_key}", operation_id="lookup", response_model=Job, responses={404: {}})
    async def lookup(idempotency_key: str) -> Job:
        """Find a job after a head restart: the live job for the key, else its latest attempt."""
        job = ledger.lookup(idempotency_key)
        if job is None:
            raise HTTPException(404, f"unknown key {idempotency_key}")
        return job

    @app.post("/jobs/{job_id}/safe_stop", operation_id="safe_stop", response_model=Job, responses={404: {}})
    async def safe_stop(job_id: str) -> Job:
        """Cancel while stoppable (counts as a pre-flight failure); after the boundary, let it finish."""
        return adapter.safe_stop(get_job(job_id))

    @app.post(
        "/jobs/{job_id}/release", operation_id="release", response_model=Job,
        responses={404: {}, 409: {"description": "Job not finished yet, or failed in protocol or stopped and not resolved"}},
    )
    async def release(job_id: str) -> Job:
        """The finished plate is off the deck: unlock the device. Called by the person who unloads it. Idempotent."""
        job = get_job(job_id)
        if job.state not in FINAL:
            raise HTTPException(409, f"job {job_id} is {job.state}; safe_stop it first")
        if job.released_at is None:
            held = ledger.held(job.device_id)
            if held and held.job_id == job_id:
                await adapter.unload(adapter.by_id[job.device_id], "release")
            else:  # never held the device (a pre-flight failure): nothing on the deck to take off
                ledger.update(job_id, released_at=now())
        return get_job(job_id)

    # --- operator API ---

    @app.get("/devices", tags=["operator"])
    async def list_devices() -> list[DeviceView]:
        """What each device can do, and whether it's free and ready."""
        return [adapter.view(d) for d in devices]

    @app.get("/queue", tags=["operator"])
    async def queue() -> list[QueuedJob]:
        """Queued jobs, oldest first, each with what it waits for."""
        return [QueuedJob(**j.model_dump(), waiting_for=adapter.waiting_for(j)) for j in ledger.queued()]

    @app.post("/devices/{device_id}/load", tags=["operator"], responses={404: {}, 409: {}})
    async def load(device_id: str, req: Load) -> DeviceView:
        """Plate on the deck, deck checked: the device takes that plate's queued jobs."""
        device = get_device(device_id)
        await adapter.load(device, req.plate, req.by)
        return adapter.view(device)

    @app.post("/devices/{device_id}/unload", tags=["operator"], responses={404: {}, 409: {}})
    async def unload(device_id: str, who: Operator | None = None) -> DeviceView:
        """Plate off the deck: releases the job holding the device, if any."""
        device = get_device(device_id)
        await adapter.unload(device, (who or Operator()).by)
        return adapter.view(device)

    @app.post("/devices/{device_id}/estop", tags=["operator"], responses={404: {}, 409: {}})
    async def estop(device_id: str, who: Operator | None = None) -> DeviceView:
        """Stop the running job at once; it ends `stopped` and keeps the device until unloaded."""
        device = get_device(device_id)
        adapter.estop(device, (who or Operator()).by)
        return adapter.view(device)

    @app.post("/jobs/{job_id}/resolve", tags=["operator"], responses={404: {}, 409: {}})
    async def resolve(job_id: str, req: Resolve) -> Job:
        """Record a failed or stopped job's real outcome after checking the deck."""
        return await adapter.resolve(get_job(job_id), req)

    @app.get("/deck", include_in_schema=False)
    async def deck() -> HTMLResponse:
        return HTMLResponse((Path(__file__).parent / "deck.html").read_text())

    return app


def contract_spec(app: FastAPI) -> dict:
    """The device-job contract: the app's OpenAPI spec without the operator API."""
    routes = [r for r in app.routes if "operator" not in getattr(r, "tags", [])]
    return get_openapi(title=app.title, version=app.version, routes=routes)


def device_specs(spec: str, prefix: str) -> list[tuple[str, str, str | None]]:
    """NF_LAB_DEVICES as (device_id, capability, queue) triples: a count of liquid handlers named
    <prefix>-1…, or `capability[/queue]:count,…` named <queue or capability>-1…."""
    if spec.isdigit():
        return [(f"{prefix}-{i}", "liquid-handling", None) for i in range(1, int(spec) + 1)]
    out = []
    for part in spec.split(","):
        pool, _, count = part.strip().rpartition(":")
        capability, _, queue = pool.partition("/")
        if not (capability and count.isdigit()):
            raise ValueError(f"NF_LAB_DEVICES: {part.strip()!r} isn't capability[/queue]:count")
        out += [(f"{queue or capability}-{i}", capability, queue or None) for i in range(1, int(count) + 1)]
    if len({d[0] for d in out}) < len(out):
        raise ValueError(f"NF_LAB_DEVICES: {spec!r} names a device twice: give each pool its own queue")
    return out


def main() -> FastAPI:
    """`uvicorn --factory` entry point: a pool of simulated devices.

    NF_LAB_DEVICES: how many liquid handlers (default 1), named fake-1… or plr-sim-1…; or
    pools as `capability[/queue]:count,…`, e.g. `liquid-handling/opentrons:2,liquid-handling/hamilton:1`.
    NF_LAB_BACKEND=fake (scripted, default) or plr (PyLabRobot simulation: `plate-reading`
    devices are plate readers, the others liquid handlers);
    with plr, NF_LAB_VISUALIZER=1 serves PyLabRobot's browser view of the deck.
    NF_LAB_SIMULATED_OPERATOR=1 or 0: default 1 for fake (tests, CI), 0 for plr,
    so a person loads and unloads plates on the deck page.
    NF_LAB_TOKEN: every request must carry it as a bearer token; unset: no check.
    """
    step = float(os.environ.get("NF_LAB_STEP", "1.0"))
    plr = os.environ.get("NF_LAB_BACKEND", "fake") == "plr"
    if plr:
        from .plr import PlrReaderBackend, PlrSimBackend  # imported only when used: PyLabRobot is heavy
        prefix = "plr-sim"
        def backend(capability: str) -> Backend:
            binding = PlrReaderBackend if capability == "plate-reading" else PlrSimBackend
            return binding(step=step, visualize=os.environ.get("NF_LAB_VISUALIZER") == "1")
    else:
        prefix, backend = "fake", lambda _: FakeBackend(step=step)
    return create_app(
        os.environ.get("NF_LAB_LEDGER", "ledger.sqlite"),
        [Device(device_id, capability, backend(capability), queue=queue,
                silent_after=float(os.environ.get("NF_LAB_SILENT_AFTER", "600")),
                simulated_operator=os.environ.get("NF_LAB_SIMULATED_OPERATOR", "0" if plr else "1") == "1")
         for device_id, capability, queue in device_specs(os.environ.get("NF_LAB_DEVICES", "1"), prefix)],
        token=os.environ.get("NF_LAB_TOKEN") or None,
    )


if __name__ == "__main__":
    print(json.dumps(contract_spec(create_app(":memory:", [])), indent=2))
