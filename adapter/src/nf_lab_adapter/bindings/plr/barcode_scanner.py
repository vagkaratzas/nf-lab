"""`barcode-reading`: a plate barcode scanner, on a simulated backend of our own (PyLabRobot
0.2.2 has the Keyence driver but no simulator)."""

import asyncio
import logging

from pylabrobot.barcode_scanners import BarcodeScanner
from pylabrobot.barcode_scanners.backend import BarcodeScannerBackend
from pylabrobot.resources import cor_96_wellplate_360uL_Fb
from pylabrobot.resources.barcode import Barcode

from .. import Progress, scanned
from ...contract import Job, State
from ._base import PlrMachine

log = logging.getLogger(__name__)


class SimulatedBarcodeScannerBackend(BarcodeScannerBackend):
    """PyLabRobot's scanner interface; it reads `label`, the barcode on the plate in front of it."""

    label: str | None = None

    async def setup(self): log.info("scanner: setup")
    async def stop(self): log.info("scanner: stop")

    async def scan_barcode(self) -> Barcode:
        if self.label is None:
            raise RuntimeError("no barcode read")
        return Barcode(data=self.label, symbology="Code 128 (Subset B and C)", position_on_resource="front")


class PlrBarcodeScannerBackend(PlrMachine):
    """`plate_scan`: read the plate's barcode and check it is the job's plate, the check that a
    person loaded the plate the ledger thinks is there. A mismatch fails the job before anything
    happens. Scanning changes nothing, so the job never crosses the stop boundary: once a person
    puts the right plate there, it can run again."""

    PROTOCOLS = {"plate_scan"}

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)  # no viewer: a scanner isn't a PyLabRobot Resource
        self.scanner: BarcodeScanner | None = None
        self.plate = None

    async def setup(self) -> None:
        scanner = BarcodeScanner(SimulatedBarcodeScannerBackend())
        await scanner.setup()
        self.scanner = scanner

    async def load(self, plate: str | None) -> None:
        self.plate = cor_96_wellplate_360uL_Fb("plate") if plate else None
        self.scanner.backend.label = plate  # the simulated plate carries the ID the person loaded

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        if self.plate is None:
            raise RuntimeError("no plate in front of the scanner")
        self._check(job, self.plate)
        progress(state=State.running)
        await asyncio.sleep(self.step)
        barcode = (await self.scanner.scan()).data
        progress()
        if barcode != job.plate:
            raise RuntimeError(f"scanned {barcode}, but the job is for {job.plate}")
        return {**scanned(job), "barcode": barcode}  # what the scanner read
