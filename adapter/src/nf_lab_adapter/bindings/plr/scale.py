"""`weighing`: a simulated balance (chatterbox backend)."""

import asyncio

from pylabrobot.resources import Coordinate, cor_96_wellplate_360uL_Fb
from pylabrobot.scales import Scale, ScaleChatterboxBackend

from .. import Progress, weighed
from ...contract import Job, State
from ._base import PlrMachine


class SimulatedScaleBackend(ScaleChatterboxBackend):
    """The chatterbox with a settable reading: PyLabRobot 0.2.2 keeps it private, with no setter."""

    weight = 0.0

    async def read_weight(self) -> float:
        await super().read_weight()
        return self.weight


class PlrScaleBackend(PlrMachine):
    """`plate_weigh`: weigh the plate (gravimetric QC). A person's load puts it on the pan, an
    unload takes it off. Weighing changes nothing, so the job never crosses the stop boundary:
    a failed or safe-stopped weighing can run again."""

    PROTOCOLS = {"plate_weigh"}

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)
        self.scale: Scale | None = None

    async def setup(self) -> None:
        scale = Scale("scale", size_x=200, size_y=200, size_z=100, backend=SimulatedScaleBackend())
        await scale.setup()
        await self._serve(scale)
        self.scale = scale

    async def load(self, plate: str | None) -> None:
        self._swap_plate(self.scale, None)
        if plate:
            await self.scale.zero()  # zeroed empty, before the plate goes on
            self.scale.assign_child_resource(cor_96_wellplate_360uL_Fb("plate"), location=Coordinate.zero())

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        if not self.scale.children:
            raise RuntimeError("no plate on the scale")
        self._check(job, self.scale.children[0])
        progress(state=State.running)
        self.scale.backend.weight = weighed(job)["weight_g"]  # what the simulated plate weighs
        await asyncio.sleep(self.step)  # settle
        progress()
        return {**weighed(job), "weight_g": await self.scale.read_weight()}  # what the scale read
