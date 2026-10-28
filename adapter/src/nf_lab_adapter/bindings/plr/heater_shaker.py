"""`heating-shaking`: a simulated heater-shaker (chatterbox backend)."""

from pylabrobot.heating_shaking import HeaterShaker, HeaterShakerChatterboxBackend
from pylabrobot.resources import Coordinate

from .. import Progress
from ._base import PlrPlateHolder


class PlrHeaterShakerBackend(PlrPlateHolder):
    """`plate_lysis`: heat the plate to the lysis temperature and shake it for the hold."""

    PROTOCOLS = {"plate_lysis"}

    def make(self) -> HeaterShaker:
        return HeaterShaker("heater_shaker", size_x=127.76, size_y=85.48, size_z=60,
                            backend=HeaterShakerChatterboxBackend(), child_location=Coordinate.zero())

    async def treat(self, program: dict, progress: Progress) -> None:
        hs = self.machine
        await hs.set_temperature(program["temperature"])
        await hs.wait_for_temperature()  # the chatterbox is there at once
        progress()
        await hs.shake(program["speed_rpm"])  # no duration: PyLabRobot would sleep it out
        await self._hold(progress)
        await hs.stop_shaking()
        await hs.deactivate()
        await hs.unlock_plate()
