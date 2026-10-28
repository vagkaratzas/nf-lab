"""`liquid-handling`: a simulated liquid handler (Hamilton STARlet deck, chatterbox backend).

One simulated robot and deck live as long as the adapter: tips, a source plate holding the
samples, a buffer plate for controls and blanks (and lysis buffer), two destination plates and
a 96-well PCR plate for eluates. A person's load (`load(plate)`) resets the deck to full source
and buffer wells and empty destinations; an unload (`load(None)`) empties every well.
"""

import asyncio

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import (
    PLT_CAR_L5AC_A00, azenta_96_wellplate_200uL_Vb_4titudeframestar, TIP_CAR_480_A00, STARLetDeck, cor_96_wellplate_360uL_Fb,
    hamilton_96_tiprack_1000uL_filter,
)

from .. import ELUATE_UL, EXTRACTIONS, LYSIS_UL, SPLIT_UL, Progress, aliquots, extracted
from ...contract import Job, State, WellType
from ._base import PlrMachine

CHANNELS = 8
SAMPLE_UL = 300.0  # what each source well holds (of 360 µL): the visualizer shades by volume / max


class PlrLiquidHandlerBackend(PlrMachine):
    """Liquid handling on a deck of tips, a sample (source) plate, a buffer plate, two
    empty plates and an empty PCR plate (200 µL wells) for eluates. One column at a time, one channel per well; wells not in the
    plate map are never touched.

    - `plate_transfer`: fill each plate-map well of the destination plate with
      `volume_ul` from the same well of its source: the sample plate for samples,
      the buffer plate for controls and blanks.
    - `plate_split`: the same, `SPLIT_UL` into each of two aliquot plates (the
      destination and the second plate), from one aspirate.
    - `rna_extraction`, `dna_extraction`: the plate is an aliquot, controls and blanks
      included, so every plate-map well is on the sample plate. Add `LYSIS_UL` of
      lysis buffer (from the buffer plate) to each, then move `ELUATE_UL` of the lysate
      to the PCR plate, ready for the thermocycler. The yield is simulated (`concentration`)."""

    PROTOCOLS = {"plate_transfer", "plate_split", *EXTRACTIONS}

    def __init__(self, step: float = 0.5, volume_ul: float = 150.0, visualize: bool = False):
        super().__init__(step, visualize)
        self.volume_ul = volume_ul
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
        await self._serve(lh)
        self.lh = lh

    async def load(self, plate: str | None) -> None:
        tips, src, buf, dst, dst2, eluate = self.deck
        for w in src.children + buf.children:
            w.tracker.set_volume(SAMPLE_UL if plate else 0)
        for w in dst.children + dst2.children + eluate.children:
            w.tracker.set_volume(0)
        # Fresh tips, as a person setting up the deck would: a run stopped between aspirate and
        # dispense puts its tips back in the rack still holding liquid, and they can't be dropped.
        for channel in self.lh.head.values():
            channel.clear()
        for spot in tips.children:
            if spot.tracker.has_tip:
                spot.tracker.get_tip().tracker.set_volume(0)

    async def read_result(self, job: Job) -> dict:
        _, _, _, dst, dst2, eluate = self.deck
        held = lambda plate, w: plate.get_item(f"{w.row}{w.column}").tracker.get_used_volume()
        if job.protocol == "plate_split":
            return aliquots(job, lambda i, w: held(dst if i == 1 else dst2, w))
        if job.protocol in EXTRACTIONS:
            return extracted(job, lambda w: held(eluate, w))
        return {"plate": job.plate, "wells": [{**w.model_dump(mode="json"), "volume_ul": held(dst, w)} for w in job.wells]}

    async def run(self, job: Job, progress: Progress) -> dict:
        lh, (tips, src, buf, dst, dst2, eluate) = self.lh, self.deck
        for channel in lh.head.values():
            channel.clear()  # a cancelled job may have left tips on the channels
        await asyncio.sleep(self.step)  # deck check
        progress()
        # pre-flight: everything checkable before material is touched
        self._check(job, dst)
        ids = [f"{w.row}{w.column}" for w in job.wells]
        drawn = {"plate_transfer": self.volume_ul, "plate_split": 2 * SPLIT_UL}.get(job.protocol, ELUATE_UL)
        if drawn > SAMPLE_UL:
            raise ValueError(f"{drawn} µL asked, source wells hold {SAMPLE_UL} µL")
        progress(state=State.running)

        # On cancellation the next await raises: commands simply stop, nothing is aborted.
        extraction = job.protocol in EXTRACTIONS
        batches: dict[tuple[int, bool], list[str]] = {}  # (column, from the sample plate) -> wells
        for w, i in zip(job.wells, ids):  # wells come in row, column order (contract)
            batches.setdefault((w.column, extraction or w.type == WellType.sample), []).append(i)
        for (_, is_sample), batch in sorted(batches.items()):
            source = src if is_sample else buf
            vols = lambda v: [v] * len(batch)
            await self._op(progress, lh.pick_up_tips(tips.get_items(batch)))
            progress(stoppable=False)  # first aspirate: material is now touched
            if job.protocol == "plate_transfer":
                await self._op(progress, lh.aspirate(source.get_items(batch), vols=vols(self.volume_ul)))
                await self._op(progress, lh.dispense(dst.get_items(batch), vols=vols(self.volume_ul)))
            elif job.protocol == "plate_split":
                await self._op(progress, lh.aspirate(source.get_items(batch), vols=vols(2 * SPLIT_UL)))
                await self._op(progress, lh.dispense(dst.get_items(batch), vols=vols(SPLIT_UL)))
                await self._op(progress, lh.dispense(dst2.get_items(batch), vols=vols(SPLIT_UL)))
            else:  # extraction: lysis, then the lysate to the PCR plate (one tip per well: same sample)
                await self._op(progress, lh.aspirate(buf.get_items(batch), vols=vols(LYSIS_UL)))
                await self._op(progress, lh.dispense(src.get_items(batch), vols=vols(LYSIS_UL)))
                await self._op(progress, lh.aspirate(src.get_items(batch), vols=vols(ELUATE_UL)))
                await self._op(progress, lh.dispense(eluate.get_items(batch), vols=vols(ELUATE_UL)))
            await self._op(progress, lh.drop_tips(tips.get_items(batch)))

        return await self.read_result(job)

    @staticmethod
    def _load_deck(lh: LiquidHandler):
        tip_car = TIP_CAR_480_A00("tip_carrier")
        tip_car[0] = tips = hamilton_96_tiprack_1000uL_filter("tips")
        lh.deck.assign_child_resource(tip_car, rails=3)
        plate_car = PLT_CAR_L5AC_A00("plate_carrier")
        plate_car[0] = src = cor_96_wellplate_360uL_Fb("source")
        plate_car[1] = buf = cor_96_wellplate_360uL_Fb("buffer")
        plate_car[2] = dst = cor_96_wellplate_360uL_Fb("destination")
        plate_car[3] = dst2 = cor_96_wellplate_360uL_Fb("second_destination")
        plate_car[4] = eluate = azenta_96_wellplate_200uL_Vb_4titudeframestar("eluate")
        lh.deck.assign_child_resource(plate_car, rails=9)
        return tips, src, buf, dst, dst2, eluate
