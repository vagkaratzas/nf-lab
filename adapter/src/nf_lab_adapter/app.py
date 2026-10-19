"""Device adapter: the device-job contract over HTTP, one worker per device.

Run:     uv run uvicorn nf_lab_adapter.app:main --factory
Export:  uv run python -m nf_lab_adapter.app > openapi.json
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Response

from .backends import Backend, DeviceStopped, FakeBackend
from .contract import FINAL, Job, State, SubmitRequest
from .ledger import KeyConflict, Ledger, now


@dataclass
class Device:
    device_id: str
    capability: str
    backend: Backend
    # Seconds without a device report before the job fails as `device_silent`.
    # Per device: set it longer than the slowest step's reporting gap (incubators).
    silent_after: float = 600.0


class Adapter:
    def __init__(self, ledger: Ledger, devices: list[Device]):
        self.ledger = ledger
        # one device per capability; pools are DESIGN open question 9.
        self.devices = {d.capability: d for d in devices}
        self.wake = {d.device_id: asyncio.Event() for d in devices}
        self.running: dict[str, asyncio.Task] = {}

    async def worker(self, device: Device) -> None:
        """Run the device's queued jobs one at a time, in submit order."""
        wake = self.wake[device.device_id]
        while True:
            wake.clear()
            job = self.ledger.next_runnable(device.device_id)
            if job is None:  # nothing queued, or the device is locked until release
                await wake.wait()
                continue
            await self.run_job(device, job)

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
                    # Stop driving a device we can no longer hear. The lock stays held:
                    # a human checks the deck before release.
                    task.cancel()
                    await asyncio.wait({task})
                    self._fail(job.job_id, "device_silent")
                    return
            self.ledger.update(job.job_id, state=State.done, result=task.result())
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # adapter shutting down: the ledger settles this job on restart
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


def create_app(ledger_path: str, devices: list[Device]) -> FastAPI:
    ledger = Ledger(ledger_path)
    adapter = Adapter(ledger, devices)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        workers = [asyncio.create_task(adapter.worker(d)) for d in devices]
        yield
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    app = FastAPI(title="nf-lab device adapter", version="0.1.0", lifespan=lifespan)

    def get_job(job_id: str) -> Job:
        job = ledger.get(job_id)
        if job is None:
            raise HTTPException(404, f"unknown job {job_id}")
        return job

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
        """Find a job after a head restart: the live job for the key, else its latest pre-flight failure."""
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
        """Unlock the device once outputs are collected. Idempotent."""
        job = get_job(job_id)
        if job.state not in FINAL:
            raise HTTPException(409, f"job {job_id} is {job.state}; safe_stop it first")
        if job.released_at is None:
            job = ledger.update(job_id, released_at=now())
            adapter.wake[job.device_id].set()
        return job

    return app


def main() -> FastAPI:
    """`uvicorn --factory` entry point: one simulated liquid handler.

    NF_LAB_BACKEND=fake (scripted, default) or plr (PyLabRobot simulation);
    with plr, NF_LAB_VISUALIZER=1 serves PyLabRobot's browser view of the deck.
    """
    step = float(os.environ.get("NF_LAB_STEP", "1.0"))
    if os.environ.get("NF_LAB_BACKEND", "fake") == "plr":
        from .plr import PlrSimBackend  # imported only when used: PyLabRobot is heavy
        device_id, backend = "plr-sim-1", PlrSimBackend(step=step, visualize=os.environ.get("NF_LAB_VISUALIZER") == "1")
    else:
        device_id, backend = "fake-1", FakeBackend(step=step)
    return create_app(
        os.environ.get("NF_LAB_LEDGER", "ledger.sqlite"),
        [Device(device_id, "liquid-handling", backend,
                silent_after=float(os.environ.get("NF_LAB_SILENT_AFTER", "600")))],
    )


if __name__ == "__main__":
    print(json.dumps(create_app(":memory:", []).openapi(), indent=2))
