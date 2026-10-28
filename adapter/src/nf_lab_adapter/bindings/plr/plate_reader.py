"""`plate-reading`: a simulated plate reader (chatterbox backend)."""

import asyncio

from pylabrobot.plate_reading import PlateReader, PlateReaderChatterboxBackend
from pylabrobot.resources import cor_96_wellplate_360uL_Fb

from .. import ABSORBANCE_NM, Progress, absorbance, reading
from ...contract import Job, State
from ._base import PlrMachine


class PlrPlateReaderBackend(PlrMachine):
    """`plate_read`: read the absorbance of each plate-map well. A person's load puts a
    plate in the reader, an unload takes it out. Reading moves no liquid, so the job
    never crosses the stop boundary: a failed or safe-stopped read can run again
    (an e-stopped one after a person's Failed)."""

    PROTOCOLS = {"plate_read"}

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)
        self.reader: PlateReader | None = None

    async def setup(self) -> None:
        reader = PlateReader("plate_reader", size_x=127.76, size_y=85.48, size_z=60,
                             backend=PlateReaderChatterboxBackend())
        await reader.setup()
        await self._serve(reader)
        self.reader = reader

    async def load(self, plate: str | None) -> None:
        self._swap_plate(self.reader, cor_96_wellplate_360uL_Fb("plate") if plate else None)

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        plate = self.reader.get_plate()
        rows = self._check(job, plate)
        progress(state=State.running)
        # The chatterbox reads a fixed matrix: fill it with the simulated readings first.
        dummy = [[0.0] * plate.num_items_x for _ in range(plate.num_items_y)]
        for w in job.wells:
            dummy[rows.index(w.row)][w.column - 1] = absorbance(w)
        self.reader.backend.dummy_absorbance = dummy
        await self.reader.close()
        await asyncio.sleep(self.step)
        progress()
        data = (await self.reader.read_absorbance(
            ABSORBANCE_NM, wells=plate.get_items([f"{w.row}{w.column}" for w in job.wells]),
            use_new_return_type=True))[0]["data"]
        await asyncio.sleep(self.step)
        await self.reader.open()
        progress()
        return {**reading(job), "wells": [{**w.model_dump(mode="json"), "absorbance": data[rows.index(w.row)][w.column - 1]}
                                           for w in job.wells]}  # what the reader read
