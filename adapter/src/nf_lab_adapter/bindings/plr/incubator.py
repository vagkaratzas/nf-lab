"""`incubation`: a simulated plate incubator (chatterbox backend): one rack, and a loading tray
the plate goes in and out by."""

import asyncio

from pylabrobot.resources import Coordinate, Resource
from pylabrobot.storage import Incubator, IncubatorChatterboxBackend
from pylabrobot.storage.cytomat.racks import cytomat_rack_17mm_28

from .. import PROGRAMS, Progress
from ...contract import Job
from ._base import PlrPlateHolder


class PlrIncubatorBackend(PlrPlateHolder):
    """`plate_incubate`: hold the plate in the incubator at the program's temperature. A person's
    load puts the plate on the loading tray. Pre-flight heats the incubator and picks a free rack
    site, so taking the plate in starts the treatment: that is the stop boundary. The plate comes
    back to the tray at the end."""

    PROTOCOLS = {"plate_incubate"}
    WARMUP = 10  # simulated steps to reach the temperature before pre-flight fails
    TOLERANCE = 0.5  # °C

    def make(self) -> Incubator:
        return Incubator(IncubatorChatterboxBackend(), "incubator", size_x=600, size_y=600, size_z=600,
                         racks=[cytomat_rack_17mm_28("rack")], loading_tray_location=Coordinate.zero())

    @property
    def holder(self) -> Resource:
        return self.machine.loading_tray

    async def load(self, plate: str | None) -> None:
        for site in self.machine.racks[0].sites.values():  # a plate left inside by a failed run
            self._swap_plate(site, None)
        await super().load(plate)

    async def ready(self, job: Job, progress: Progress) -> None:
        target = PROGRAMS[job.protocol]["temperature"]
        await self.machine.set_temperature(target)
        # PyLabRobot 0.2.2's Incubator has no wait_for_temperature: poll it.
        for _ in range(self.WARMUP):
            if abs(await self.machine.get_temperature() - target) <= self.TOLERANCE:
                break
            await asyncio.sleep(self.step)
            progress()
        else:
            raise RuntimeError(f"the incubator didn't reach {target} °C")
        await super().ready(job, progress)  # the door opens to the tray
        self.site = self.machine.find_smallest_site_for_plate(self.holder.resource)  # raises if none is free

    async def treat(self, program: dict, progress: Progress) -> None:
        await self.machine.take_in_plate(self.site)
        await self._hold(progress)
        await self.machine.fetch_plate_to_loading_tray("plate")
