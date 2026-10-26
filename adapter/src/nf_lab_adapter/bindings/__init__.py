"""Backend bindings: start, report on and stop a job on one kind of device.

A binding is one async `run(job, progress)`:
- call `progress()` whenever the device reports (refreshes the heartbeat);
  `progress(state=State.running)` to change phase; `progress(stoppable=False)`
  just before the first liquid moves (the stop boundary). A binding that moves
  no liquid (a plate reader) never crosses it: its failures are retryable;
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

import zlib
from typing import Protocol

from ..contract import Job, State, Well, WellType

ABSORBANCE_NM = 450


def absorbance(well: Well) -> float:
    """A simulated reading: controls and blanks near background, each sample its own value
    from its ID, so a re-read gives the same number."""
    if well.type != WellType.sample:
        return {WellType.negative_control: 0.05, WellType.blank: 0.04}[well.type]
    return round(0.2 + zlib.crc32(well.sample.encode()) % 1300 / 1000, 3)


def reading(job: Job) -> dict:
    """`plate_read`'s outputs: the absorbance of each plate-map well."""
    return {"plate": job.plate, "wavelength_nm": ABSORBANCE_NM,
            "wells": [{**w.model_dump(mode="json"), "absorbance": absorbance(w)} for w in job.wells]}


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
