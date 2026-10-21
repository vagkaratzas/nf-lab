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

Two more calls come from a person (or the simulated operator), never from a job:
- `load(plate)`: the person put that plate on the deck and checked the deck;
  `load(None)`: they took it off. A simulation sets its deck up here.
- `read_result(job)`: the job's outputs as the deck shows them now. Used when a
  person resolves a failed job as done after checking the deck.
"""

import asyncio
from typing import Protocol

from .contract import Job, State


class DeviceStopped(Exception):
    """A human stopped the device (e-stop, vendor UI)."""


class Progress(Protocol):
    def __call__(self, *, state: State | None = None, stoppable: bool | None = None) -> None: ...


class Backend(Protocol):
    PROTOCOLS: set[str]  # the protocols this binding runs
    viewer: str | None  # URL of a live view of the deck, if the binding serves one

    async def setup(self) -> None: ...  # once, when the adapter starts
    async def run(self, job: Job, progress: Progress) -> dict: ...
    async def load(self, plate: str | None) -> None: ...
    async def read_result(self, job: Job) -> dict: ...


class FakeBackend:
    """Scripted device for tests. The protocol name picks the script."""

    PROTOCOLS = {"ok", "preflight_fail", "preflight_once", "protocol_fail", "silent", "estop"}
    viewer = None

    def __init__(self, step: float = 0.05):
        self.step = step  # seconds per simulated step

    async def setup(self) -> None:
        pass

    async def load(self, plate: str | None) -> None:
        pass

    async def read_result(self, job: Job) -> dict:
        return {"wells": len(job.wells)}

    async def run(self, job: Job, progress: Progress) -> dict:
        script = job.protocol
        await asyncio.sleep(self.step)  # deck check
        progress()
        if script not in self.PROTOCOLS:
            raise ValueError(f"unknown protocol {script!r}")
        if script == "preflight_fail" or (script == "preflight_once" and job.attempt == 1):
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
        return await self.read_result(job)
