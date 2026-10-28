"""A scripted binding for tests and CI: no device, no PyLabRobot."""

import asyncio

from ..contract import Job, State
from . import READ_ONLY, RESULTS, DeviceStopped, Progress


class FakeBackend:
    """Scripted device for tests. The protocol name picks the script.

    `plate_transfer` runs like `ok` and returns PlrLiquidHandlerBackend's result shape
    (150 µL in each plate-map well), so examples/plate_transfer runs without PyLabRobot.
    Every protocol in `RESULTS` returns its PyLabRobot binding's result shape; one in
    `READ_ONLY` (`plate_read`), like a reader, never crosses the stop boundary."""

    PROTOCOLS = {"ok", "plate_transfer", *RESULTS, "preflight_fail", "preflight_once", "protocol_fail", "silent", "estop"}
    viewer = None

    def __init__(self, step: float = 0.05):
        self.step = step  # seconds per simulated step

    async def setup(self) -> None:
        pass

    async def load(self, plate: str | None) -> None:
        pass

    async def read_result(self, job: Job) -> dict:
        if job.protocol in RESULTS:
            return RESULTS[job.protocol](job)
        if job.protocol == "plate_transfer":
            return {"plate": job.plate, "wells": [{**w.model_dump(mode="json"), "volume_ul": 150.0} for w in job.wells]}
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
        if script not in READ_ONLY:
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
