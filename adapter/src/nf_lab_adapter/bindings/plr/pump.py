"""`pumping`: a simulated 8-channel pump array (chatterbox backend) over a dispensing manifold."""

import asyncio

from pylabrobot.pumps import PumpArray, PumpCalibration
from pylabrobot.pumps.chatterbox import PumpArrayChatterboxBackend
from pylabrobot.resources import cor_96_wellplate_360uL_Fb

from .. import RESULTS, Progress
from ...contract import Job, State
from ._base import PlrMachine


class PlrPumpBackend(PlrMachine):
    """`reagent_dispense`: pump the program's reagent and volume into each plate-map well. The 8 channels
    feed rows A–H; the manifold steps column by column, one pump per column. A person's load
    puts the plate under the manifold. The first pump changes the plate: the stop boundary."""

    PROTOCOLS = {"reagent_dispense"}
    SPEED = 100.0  # rpm

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)  # no viewer: a pump array isn't a PyLabRobot Resource
        self.pumps: PumpArray | None = None
        self.plate = None

    async def setup(self) -> None:
        # Simulated calibration, 1 µL per revolution, so pump_volume can turn µL into revolutions.
        pumps = PumpArray(PumpArrayChatterboxBackend(8), calibration=PumpCalibration([1.0] * 8, "revolutions"))
        await pumps.setup()
        self.pumps = pumps

    async def load(self, plate: str | None) -> None:
        self.plate = cor_96_wellplate_360uL_Fb("plate") if plate else None  # a fresh plate: empty wells

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        if self.plate is None:
            raise RuntimeError("no plate under the manifold")
        rows = self._check(job, self.plate)
        progress(state=State.running)
        await asyncio.sleep(self.step)  # prime the lines
        progress(stoppable=False)  # the first pump: reagent goes into the plate from here on
        volume = job.program["volume_ul"]
        for column in sorted({w.column for w in job.wells}):
            batch = [w for w in job.wells if w.column == column]
            await self.pumps.pump_volume(self.SPEED, use_channels=[rows.index(w.row) for w in batch], volume=volume)
            for w in batch:
                tracker = self.plate.get_item(f"{w.row}{w.column}").tracker
                tracker.set_volume(tracker.get_used_volume() + volume)
            await asyncio.sleep(self.step)
            progress()
        return await self.read_result(job)

    async def read_result(self, job: Job) -> dict:
        held = lambda w: self.plate.get_item(f"{w.row}{w.column}").tracker.get_used_volume() if self.plate else 0.0
        return {**RESULTS[job.protocol](job),
                "wells": [{**w.model_dump(mode="json"), "volume_ul": held(w)} for w in job.wells]}  # what the plate holds
