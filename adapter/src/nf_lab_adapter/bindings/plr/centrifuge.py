"""`centrifugation`: a simulated plate centrifuge (chatterbox backend)."""

from pylabrobot.centrifuge import Centrifuge
from pylabrobot.centrifuge.chatterbox import CentrifugeChatterboxBackend
from pylabrobot.resources import Resource, cor_96_wellplate_360uL_Fb

from .. import Progress
from ._base import PlrPlateHolder


class PlrCentrifugeBackend(PlrPlateHolder):
    """`plate_spin`: spin the plate to pellet what's in each well. A person's load puts the plate
    in bucket 1 and a balance plate in bucket 2; the door stays locked while it spins."""

    PROTOCOLS = {"plate_spin"}

    def make(self) -> Centrifuge:
        return Centrifuge(CentrifugeChatterboxBackend(), "centrifuge", size_x=400, size_y=400, size_z=300)

    @property
    def holder(self) -> Resource:
        return self.machine.bucket1

    async def load(self, plate: str | None) -> None:
        await super().load(plate)
        self._swap_plate(self.machine.bucket2, cor_96_wellplate_360uL_Fb("balance") if plate else None)

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.close_door()
        await self.machine.lock_door()
        await self.machine.spin(g=program["g"], duration=program["duration_s"], acceleration=1.0)
        await self._hold(progress)  # the chatterbox returns at once; a spin takes its time
        await self.machine.unlock_door()
        await self.machine.open_door()
