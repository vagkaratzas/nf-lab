"""PyLabRobot binding on the simulated (chatterbox) liquid handler.

PyLabRobot is a library with no job service, so the binding runs the protocol
in-process. Each job gets a freshly loaded simulated deck (tips, a source plate
holding the samples, an empty destination plate); a real device would have it
loaded by an operator.
"""

import asyncio

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import (
    PLT_CAR_L5AC_A00, TIP_CAR_480_A00, STARLetDeck, cor_96_wellplate_360uL_Fb,
    hamilton_96_tiprack_1000uL_filter, set_volume_tracking,
)

from .backends import Progress
from .contract import Job, State

CHANNELS = 8
SAMPLE_UL = 200.0  # what each source well holds when the deck is loaded

set_volume_tracking(True)  # process-wide: wells track volume, so a transfer is checkable


class PlrSimBackend:
    """`plate_transfer`: move `volume_ul` of each sample from the source plate to
    the same well of the destination plate, one column (8 channels) at a time."""

    PROTOCOLS = {"plate_transfer"}

    def __init__(self, step: float = 0.5, volume_ul: float = 50.0):
        self.step = step  # simulated seconds per robot operation
        self.volume_ul = volume_ul

    async def _op(self, progress: Progress, coro) -> None:
        await coro
        await asyncio.sleep(self.step)  # the chatterbox is instant; a robot isn't
        progress()  # every operation is a device report: refresh the heartbeat

    async def run(self, job: Job, progress: Progress) -> dict:
        lh = LiquidHandler(backend=LiquidHandlerChatterboxBackend(num_channels=CHANNELS), deck=STARLetDeck())
        try:
            await self._op(progress, lh.setup())
            tips, src, dst = self._load_deck(lh)
            # pre-flight: everything checkable before material is touched
            if job.protocol not in self.PROTOCOLS:
                raise ValueError(f"unknown protocol {job.protocol!r}")
            n = len(job.sample_ids)
            if n > src.num_items:
                raise ValueError(f"{n} samples don't fit one {src.num_items}-well plate")
            if self.volume_ul > SAMPLE_UL:
                raise ValueError(f"{self.volume_ul} µL asked, source wells hold {SAMPLE_UL} µL")
            progress(state=State.running)

            for col in range(0, n, CHANNELS):
                wells = slice(col, min(col + CHANNELS, n))
                await self._op(progress, lh.pick_up_tips(tips.children[wells]))
                k = len(src.children[wells])
                progress(stoppable=False)  # first aspirate: material is now touched
                await self._op(progress, lh.aspirate(src.children[wells], vols=[self.volume_ul] * k))
                await self._op(progress, lh.dispense(dst.children[wells], vols=[self.volume_ul] * k))
                await self._op(progress, lh.drop_tips(tips.children[wells]))

            moved = dst.children[:n]
            return {
                "sample_ids": job.sample_ids,
                "plate": dst.name,
                "wells": {s: w.get_identifier() for s, w in zip(job.sample_ids, moved)},
                "volume_ul": {s: w.tracker.get_used_volume() for s, w in zip(job.sample_ids, moved)},
            }
        finally:
            # On cancellation or failure: stop issuing commands. Simulation only: a real
            # backend past the stop boundary must not abort here (see backends.py).
            await lh.stop()

    @staticmethod
    def _load_deck(lh: LiquidHandler):
        tip_car = TIP_CAR_480_A00("tip_carrier")
        tip_car[0] = tips = hamilton_96_tiprack_1000uL_filter("tips")
        lh.deck.assign_child_resource(tip_car, rails=3)
        plate_car = PLT_CAR_L5AC_A00("plate_carrier")
        plate_car[0] = src = cor_96_wellplate_360uL_Fb("source")
        plate_car[1] = dst = cor_96_wellplate_360uL_Fb("destination")
        lh.deck.assign_child_resource(plate_car, rails=9)
        for w in src.children:
            w.tracker.set_volume(SAMPLE_UL)
        return tips, src, dst
