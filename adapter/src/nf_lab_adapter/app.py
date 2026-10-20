"""Device adapter: the device-job contract over HTTP, one worker per device.

Run:     uv run uvicorn nf_lab_adapter.app:main --factory
Export:  uv run python -m nf_lab_adapter.app > openapi.json

Two APIs:
- the device-job contract (submit, status, lookup, safe_stop, release), called by
  `labctl`. It's exported to openapi.json, and every adapter must serve it.
- the operator API (tag `operator`): what a person does at the deck, i.e. load
  a plate, unload it, e-stop, or resolve a failed job. `GET /deck` serves its
  buttons. A real lab may do these in the vendor UI instead, so the operator API
  isn't part of the contract.
"""

import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .backends import Backend, DeviceStopped, FakeBackend
from .contract import FINAL, Job, State, SubmitRequest
from .ledger import KeyConflict, Ledger, now

SIMULATED_OPERATOR = "simulated operator"


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


class DeviceView(BaseModel):
    device_id: str
    capability: str
    simulated_operator: bool
    plate: str | None = Field(description="Plate on the deck; None: empty")
    held: Job | None = Field(description="The job locking the device: running, or finished and not yet unloaded")
    queued: list[Job]


class Adapter:
    def __init__(self, ledger: Ledger, devices: list[Device]):
        self.ledger = ledger
        # one device per capability; pools are DESIGN open question 9.
        self.devices = {d.capability: d for d in devices}
        self.by_id = {d.device_id: d for d in devices}
        self.wake = {d.device_id: asyncio.Event() for d in devices}
        self.running: dict[str, asyncio.Task] = {}
        self.estops: dict[str, str] = {}  # job_id -> who pressed e-stop

    async def worker(self, device: Device) -> None:
        """Run the device's queued jobs one at a time, in submit order, each once its plate is loaded."""
        wake = self.wake[device.device_id]
        while True:
            wake.clear()
            if device.simulated_operator:
                await self.operate(device)
            job = self.ledger.next_runnable(device.device_id)
            if job is None:  # nothing queued for the plate on the deck, or the device is locked
                await wake.wait()
                continue
            await self.run_job(device, job)

    async def operate(self, device: Device) -> None:
        """The simulated operator's turn: unload a done job, then load the oldest queued job's plate."""
        held = self.ledger.held(device.device_id)
        if held and held.state == State.done:
            await self.unload(device, SIMULATED_OPERATOR)
            held = None
        queue = self.ledger.queued(device.device_id)
        if not held and queue and self.ledger.plate(device.device_id) != queue[0].plate:
            if self.ledger.where(queue[0].plate) is None:  # on another device: that one takes the job
                await self.load(device, queue[0].plate, SIMULATED_OPERATOR)

    async def run_job(self, device: Device, job: Job) -> None:
        job = self.ledger.update(job.job_id, state=State.preflight, started_at=now(), device_heartbeat_at=now())

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
        self.ledger.unload(device.device_id, by)
        await device.backend.load(None)
        self.wake[device.device_id].set()

    def estop(self, device: Device, by: str) -> None:
        """Stop the running job now, past the stop boundary too: a person decided. The lock stays held."""
        held = self.ledger.held(device.device_id)
        if held is None or held.job_id not in self.running:
            raise Conflict(f"nothing running on {device.device_id}")
        self.estops[held.job_id] = by
        self.running[held.job_id].cancel()

    async def resolve(self, job: Job, r: Resolve) -> Job:
        """A person checked the deck after a failed job and records what really happened."""
        if job.state not in (State.failed_in_protocol, State.stopped):
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
        self.wake[job.device_id].set()
        return job

    def view(self, device: Device) -> DeviceView:
        return DeviceView(device_id=device.device_id, capability=device.capability,
                          simulated_operator=device.simulated_operator, plate=self.ledger.plate(device.device_id),
                          held=self.ledger.held(device.device_id), queued=self.ledger.queued(device.device_id))


def create_app(ledger_path: str, devices: list[Device]) -> FastAPI:
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

    app = FastAPI(title="nf-lab device adapter", version="0.1.0", lifespan=lifespan)

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
        """Queue a job, or return the existing one for this idempotency key."""
        device = adapter.devices.get(req.capability)
        if device is None:
            raise HTTPException(404, f"no device for capability {req.capability!r}")
        try:
            job, created = ledger.submit(req, device.device_id)
        except KeyConflict:
            raise HTTPException(409, f"idempotency key {req.idempotency_key} already used with a different payload")
        if created:
            adapter.wake[device.device_id].set()
        else:
            response.status_code = 200
        return job

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
        responses={404: {}, 409: {"description": "Job not finished yet"}},
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
        return [adapter.view(d) for d in devices]

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


def main() -> FastAPI:
    """`uvicorn --factory` entry point: one simulated liquid handler.

    NF_LAB_BACKEND=fake (scripted, default) or plr (PyLabRobot simulation);
    with plr, NF_LAB_VISUALIZER=1 serves PyLabRobot's browser view of the deck.
    NF_LAB_SIMULATED_OPERATOR=1 or 0: default 1 for fake (tests, CI), 0 for plr,
    so a person loads and unloads plates on the deck page.
    """
    step = float(os.environ.get("NF_LAB_STEP", "1.0"))
    plr = os.environ.get("NF_LAB_BACKEND", "fake") == "plr"
    if plr:
        from .plr import PlrSimBackend  # imported only when used: PyLabRobot is heavy
        device_id, backend = "plr-sim-1", PlrSimBackend(step=step, visualize=os.environ.get("NF_LAB_VISUALIZER") == "1")
    else:
        device_id, backend = "fake-1", FakeBackend(step=step)
    return create_app(
        os.environ.get("NF_LAB_LEDGER", "ledger.sqlite"),
        [Device(device_id, "liquid-handling", backend,
                silent_after=float(os.environ.get("NF_LAB_SILENT_AFTER", "600")),
                simulated_operator=os.environ.get("NF_LAB_SIMULATED_OPERATOR", "0" if plr else "1") == "1")],
    )


if __name__ == "__main__":
    print(json.dumps(contract_spec(create_app(":memory:", [])), indent=2))
