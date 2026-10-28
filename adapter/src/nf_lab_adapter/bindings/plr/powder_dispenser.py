"""`powder-dispensing`: a simulated powder dispenser (chatterbox backend)."""

import asyncio

from pylabrobot.powder_dispensing import PowderDispenser
from pylabrobot.powder_dispensing.chatterbox import PowderDispenserChatterboxBackend
from pylabrobot.resources import Powder, cor_96_wellplate_360uL_Fb

from .. import Progress, powdered
from ...contract import Job, State
from ._base import PlrMachine


class PlrPowderDispenserBackend(PlrMachine):
    """`powder_dispense`: dose the program's powder and amount into each plate-map well; the result holds the
    amount the dispenser reports per well. A person's load puts the plate on its deck. The
    first dose changes the plate: the stop boundary."""

    PROTOCOLS = {"powder_dispense"}

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)  # no viewer: a powder dispenser isn't a PyLabRobot Resource
        self.dispenser: PowderDispenser | None = None
        self.plate = None

    async def setup(self) -> None:
        dispenser = PowderDispenser(PowderDispenserChatterboxBackend())
        await dispenser.setup()
        self.dispenser = dispenser

    async def load(self, plate: str | None) -> None:
        self.plate = cor_96_wellplate_360uL_Fb("plate") if plate else None

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        if self.plate is None:
            raise RuntimeError("no plate on the powder dispenser")
        self._check(job, self.plate)
        progress(state=State.running)
        await asyncio.sleep(self.step)
        progress(stoppable=False)  # the first dose: powder goes into the plate from here on
        doses = await self.dispenser.dispense(
            self.plate.get_items([f"{w.row}{w.column}" for w in job.wells]),
            [Powder(job.program["powder"])] * len(job.wells), [job.program["amount_mg"]] * len(job.wells))
        await asyncio.sleep(self.step)
        progress()
        return {**powdered(job), "wells": [{**w.model_dump(mode="json"), "amount_mg": d["actual_amount"]}
                                           for w, d in zip(job.wells, doses)]}  # what the dispenser reports
