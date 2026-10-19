"""PyLabRobot binding on the simulated (chatterbox) liquid handler.

PyLabRobot is a library with no job service, so the binding runs the protocol
in-process. One simulated robot and deck live as long as the adapter: tips, a
source plate holding the samples, an empty destination plate. Before each job
the deck is reset to that state (an operator's job on a real device), so in
simulation a free robot is also a ready one (DESIGN open question 9).

With `visualize=True`, PyLabRobot's browser view of the deck starts with the
adapter (http://127.0.0.1:1337, or the next free port; the URL is logged).
"""

import asyncio

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import (
    PLT_CAR_L5AC_A00, TIP_CAR_480_A00, STARLetDeck, cor_96_wellplate_360uL_Fb,
    hamilton_96_tiprack_1000uL_filter, set_volume_tracking,
)
from pylabrobot.visualizer import Visualizer

from .backends import Progress
from .contract import Job, State

CHANNELS = 8
SAMPLE_UL = 300.0  # what each source well holds (of 360 µL): the visualizer shades by volume / max

set_volume_tracking(True)  # process-wide: wells track volume, so a transfer is checkable


class PlrSimBackend:
    """`plate_transfer`: move `volume_ul` of each sample from the source plate to
    the same well of the destination plate, one column (8 channels) at a time."""

    PROTOCOLS = {"plate_transfer"}

    def __init__(self, step: float = 0.5, volume_ul: float = 150.0, visualize: bool = False):
        self.step = step  # simulated seconds per robot operation
        self.volume_ul = volume_ul
        self.visualize = visualize
        self.lh: LiquidHandler | None = None  # set up when the adapter starts

    async def _op(self, progress: Progress, coro) -> None:
        await coro
        await asyncio.sleep(self.step)  # the chatterbox is instant; a robot isn't
        progress()  # every operation is a device report: refresh the heartbeat

    async def setup(self) -> None:
        """Start the simulated robot, load its deck, and serve the browser view."""
        lh = LiquidHandler(backend=LiquidHandlerChatterboxBackend(num_channels=CHANNELS), deck=STARLetDeck())
        await lh.setup()
        self.deck = self._load_deck(lh)
        if self.visualize:
            await Visualizer(resource=lh, open_browser=False).setup()
        self.lh = lh

    async def _ready(self):
        """The robot with its deck reset to the loaded state."""
        tips, src, dst = self.deck
        for w in src.children:
            w.tracker.set_volume(SAMPLE_UL)
        for w in dst.children:
            w.tracker.set_volume(0)
        for channel in self.lh.head.values():
            channel.clear()  # a cancelled job may have left tips on the channels
        return self.lh, tips, src, dst

    async def run(self, job: Job, progress: Progress) -> dict:
        lh, tips, src, dst = await self._ready()
        await asyncio.sleep(self.step)  # deck check
        progress()
        # pre-flight: everything checkable before material is touched
        if job.protocol not in self.PROTOCOLS:
            raise ValueError(f"unknown protocol {job.protocol!r}")
        n = len(job.sample_ids)
        if n > src.num_items:
            raise ValueError(f"{n} samples don't fit one {src.num_items}-well plate")
        if self.volume_ul > SAMPLE_UL:
            raise ValueError(f"{self.volume_ul} µL asked, source wells hold {SAMPLE_UL} µL")
        progress(state=State.running)

        # On cancellation the next await raises: commands simply stop, nothing is aborted.
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
