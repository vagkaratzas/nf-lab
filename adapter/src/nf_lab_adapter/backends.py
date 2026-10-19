"""Backend bindings: start, report on and stop a job on one kind of device.

A binding is one async `run(job, progress)`:
- call `progress()` whenever the device reports (refreshes the heartbeat);
  `progress(state=State.running)` to change phase; `progress(stoppable=False)`
  just before the first liquid moves (the stop boundary);
- return the job's outputs as a dict;
- raise to fail. The adapter turns the error into `failed_preflight` or
  `failed_in_protocol` from the stop boundary alone; raise `DeviceStopped` for
  a human stop;
- on cancellation, stop issuing commands and re-raise. It comes from safe_stop
  (always before the boundary) or from the device going silent (either side).
  Send the device a cancel only if the binding hasn't crossed the boundary yet;
  past it, never abort: a human checks the deck.
"""

import asyncio
from typing import Protocol

from .contract import Job, State


class DeviceStopped(Exception):
    """A human stopped the device (e-stop, vendor UI)."""


class Progress(Protocol):
    def __call__(self, *, state: State | None = None, stoppable: bool | None = None) -> None: ...


class Backend(Protocol):
    async def setup(self) -> None: ...  # once, when the adapter starts
    async def run(self, job: Job, progress: Progress) -> dict: ...


class FakeBackend:
    """Scripted device for tests. The protocol name picks the script."""

    SCRIPTS = {"ok", "preflight_fail", "protocol_fail", "silent", "estop"}

    def __init__(self, step: float = 0.05):
        self.step = step  # seconds per simulated step

    async def setup(self) -> None:
        pass

    async def run(self, job: Job, progress: Progress) -> dict:
        script = job.protocol
        await asyncio.sleep(self.step)  # deck check
        progress()
        if script not in self.SCRIPTS:
            raise ValueError(f"unknown protocol {script!r}")
        if script == "preflight_fail":
            raise RuntimeError("deck check failed: tip rack missing")
        progress(state=State.running)
        await asyncio.sleep(self.step)
        progress(stoppable=False)  # first aspirate: material is now touched
        if script == "silent":
            await asyncio.Event().wait()  # device stops reporting, forever
        if script == "estop":
            raise DeviceStopped("e-stop pressed at the deck")
        await asyncio.sleep(self.step)
        progress()
        if script == "protocol_fail":
            raise RuntimeError("tip pickup failed mid-transfer")
        return {"sample_ids": job.sample_ids, "wells": len(job.sample_ids)}
