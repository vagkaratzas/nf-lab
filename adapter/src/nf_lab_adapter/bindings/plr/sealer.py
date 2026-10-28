"""`sealing` and `peeling`: a plate sealer and a seal peeler, on simulated backends of our own
(PyLabRobot 0.2.2 has their drivers, A4S and XPeel, but no simulator)."""

import logging

from pylabrobot.peeling.peeler import Peeler
from pylabrobot.peeling.backend import PeelerBackend
from pylabrobot.resources import Coordinate, Resource, ResourceHolder
from pylabrobot.sealing import Sealer
from pylabrobot.sealing.backend import SealerBackend

from .. import Progress
from ._base import PlrPlateHolder

log = logging.getLogger(__name__)


class SimulatedSealerBackend(SealerBackend):
    """PyLabRobot's sealer interface, logging only, like its chatterbox backends."""

    temperature = 20.0

    async def setup(self): log.info("sealer: setup")
    async def stop(self): log.info("sealer: stop")
    async def open(self): log.info("sealer: open")
    async def close(self): log.info("sealer: close")
    async def get_temperature(self) -> float: return self.temperature

    async def set_temperature(self, temperature: float):
        log.info("sealer: set temperature %s", temperature)
        self.temperature = temperature

    async def seal(self, temperature: int, duration: float):
        await self.set_temperature(temperature)
        log.info("sealer: seal for %s s", duration)


class SimulatedPeelerBackend(PeelerBackend):
    """PyLabRobot's peeler interface, logging only. `peel` takes XPeel's settings (adhere_time)."""

    async def setup(self): log.info("peeler: setup")
    async def stop(self): log.info("peeler: stop")
    async def restart(self): log.info("peeler: restart")
    async def peel(self, **settings): log.info("peeler: peel %s", settings)


def _stage(name: str) -> ResourceHolder:
    """Where the plate sits: the machine itself isn't a deck resource."""
    return ResourceHolder(name, size_x=127.76, size_y=85.48, size_z=0, child_location=Coordinate.zero())


class PlrSealerBackend(PlrPlateHolder):
    """`plate_seal`: heat-seal a foil onto the plate. A person's load puts it on the stage. The
    seal changes the plate (a peel is another job), so it is the stop boundary."""

    PROTOCOLS = {"plate_seal"}

    def make(self) -> Sealer:
        self.stage = _stage("sealer_stage")
        return Sealer(SimulatedSealerBackend())

    @property
    def holder(self) -> Resource:
        return self.stage

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.close()  # draws the plate in
        await self.machine.seal(program["temperature"], program["duration_s"])
        await self._hold(progress)  # the backend returns at once; heating the foil takes its time
        await self.machine.open()


class PlrPeelerBackend(PlrPlateHolder):
    """`plate_unseal`: peel the seal off the plate. A person's load puts it on the stage. The
    peel opens the wells, so it is the stop boundary."""

    PROTOCOLS = {"plate_unseal"}

    def make(self) -> Peeler:
        self.stage = _stage("peeler_stage")
        return Peeler(SimulatedPeelerBackend())

    @property
    def holder(self) -> Resource:
        return self.stage

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.peel(adhere_time=program["adhere_time_s"])
        await self._hold(progress)
