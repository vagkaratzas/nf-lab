"""`tilting`: a simulated plate tilter (chatterbox backend)."""

from pylabrobot.resources import Coordinate
from pylabrobot.tilting import Tilter
from pylabrobot.tilting.chatterbox import TilterChatterboxBackend

from .. import Progress
from ._base import PlrPlateHolder


class PlrTilterBackend(PlrPlateHolder):
    """`plate_tilt`: tilt the plate so each well's liquid pools to one side, hold, then level it.
    shortcut: a tilt alone; in a lab it's part of a liquid-handling protocol (aspirate the last
    of a well), which needs the tilter on the handler's deck."""

    PROTOCOLS = {"plate_tilt"}

    def make(self) -> Tilter:
        return Tilter("tilter", size_x=127.76, size_y=85.48, size_z=40, backend=TilterChatterboxBackend(),
                      hinge_coordinate=Coordinate.zero(), child_location=Coordinate.zero())

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.set_angle(program["angle_deg"])
        await self._hold(progress)
        await self.machine.set_angle(0)
