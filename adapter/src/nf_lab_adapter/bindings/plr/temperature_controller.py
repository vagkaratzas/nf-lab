"""`temperature-control`: a simulated heat block (chatterbox temperature controller)."""

from pylabrobot.resources import Coordinate
from pylabrobot.temperature_controlling import TemperatureController, TemperatureControllerChatterboxBackend

from .. import Progress
from ._base import PlrPlateHolder


class PlrTemperatureControllerBackend(PlrPlateHolder):
    """`heat_inactivation`: hold the plate at the inactivation temperature, then let the block cool."""

    PROTOCOLS = {"heat_inactivation"}

    def make(self) -> TemperatureController:
        return TemperatureController("heat_block", size_x=127.76, size_y=85.48, size_z=40,
                                     backend=TemperatureControllerChatterboxBackend(), child_location=Coordinate.zero())

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.set_temperature(program["temperature"])
        await self.machine.wait_for_temperature()  # the chatterbox is there at once
        await self._hold(progress)
        await self.machine.deactivate()
