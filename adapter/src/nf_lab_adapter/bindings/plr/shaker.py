"""`shaking`: a simulated plate shaker (chatterbox backend)."""

from pylabrobot.resources import Coordinate
from pylabrobot.shaking import Shaker, ShakerChatterboxBackend

from .. import Progress
from ._base import PlrPlateHolder


class PlrShakerBackend(PlrPlateHolder):
    """`plate_mix`: shake the plate to mix what's in each well (the plate locked while it shakes)."""

    PROTOCOLS = {"plate_mix"}

    def make(self) -> Shaker:
        return Shaker("shaker", size_x=127.76, size_y=85.48, size_z=50,
                      backend=ShakerChatterboxBackend(), child_location=Coordinate.zero())

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.shake(program["speed_rpm"])  # no duration: PyLabRobot would sleep it out
        await self._hold(progress)
        await self.machine.stop_shaking()
        await self.machine.unlock_plate()
