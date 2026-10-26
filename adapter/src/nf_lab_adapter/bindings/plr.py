"""PyLabRobot bindings on simulated (chatterbox) machines: a liquid handler, a plate reader and a thermocycler.

PyLabRobot is a library with no job service, so the binding runs the protocol
in-process. One simulated robot and deck live as long as the adapter: tips, a
source plate holding the samples, a buffer plate for controls and blanks (and
lysis buffer), two destination plates and a 96-well PCR plate for eluates. A person's
load (`load(plate)`) resets the deck to full source and buffer wells and empty destinations; an unload (`load(None)`)
empties every well. The labware carries no plate ID: the adapter's ledger
keeps which plate is on the deck.

With `visualize=True`, PyLabRobot's browser view of the deck starts with the
adapter (http://127.0.0.1:1337, or the next free port; the URL is logged).
"""

import asyncio

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.plate_reading import PlateReader, PlateReaderChatterboxBackend
from pylabrobot.resources import (
    PLT_CAR_L5AC_A00, Coordinate, azenta_96_wellplate_200uL_Vb_4titudeframestar, TIP_CAR_480_A00, STARLetDeck, cor_96_wellplate_360uL_Fb,
    hamilton_96_tiprack_1000uL_filter, set_volume_tracking,
)
from pylabrobot.thermocycling import Thermocycler, ThermocyclerChatterboxBackend
from pylabrobot.visualizer import Visualizer

from . import (
    ABSORBANCE_NM, ELUATE_UL, EXTRACTIONS, LYSIS_UL, PCR, SPLIT_UL, Progress, absorbance, aliquots, amplified,
    extracted, reading,
)
from ..contract import Job, State, WellType

CHANNELS = 8
SAMPLE_UL = 300.0  # what each source well holds (of 360 µL): the visualizer shades by volume / max

set_volume_tracking(True)  # process-wide: wells track volume, so a transfer is checkable


class PlrLiquidHandlerBackend:
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
        self.step = step  # simulated seconds per robot operation
        self.volume_ul = volume_ul
        self.visualize = visualize
        self.lh: LiquidHandler | None = None  # set up when the adapter starts
        self.viewer: str | None = None

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
            v = Visualizer(resource=lh, open_browser=False)
            await v.setup()
            self.viewer = f"http://{v.host}:{v.fs_port}"  # the port it found free
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
        if job.protocol not in self.PROTOCOLS:
            raise ValueError(f"unknown protocol {job.protocol!r}")
        ids = [f"{w.row}{w.column}" for w in job.wells]
        rows = "ABCDEFGHIJKLMNOP"[:dst.num_items_y]
        if off := [i for w, i in zip(job.wells, ids) if w.row not in rows or w.column > dst.num_items_x]:
            raise ValueError(f"wells {', '.join(off)} aren't on a {dst.num_items}-well plate")
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


class PlrPlateReaderBackend:
    """`plate_read`: read the absorbance of each plate-map well. A person's load puts a
    plate in the reader, an unload takes it out. Reading moves no liquid, so the job
    never crosses the stop boundary: a failed or safe-stopped read can run again
    (an e-stopped one after a person's Failed)."""

    PROTOCOLS = {"plate_read"}

    def __init__(self, step: float = 0.5, visualize: bool = False):
        self.step = step
        self.visualize = visualize
        self.reader: PlateReader | None = None
        self.viewer: str | None = None

    async def setup(self) -> None:
        reader = PlateReader("plate_reader", size_x=127.76, size_y=85.48, size_z=60,
                             backend=PlateReaderChatterboxBackend())
        await reader.setup()
        if self.visualize:
            v = Visualizer(resource=reader, open_browser=False)
            await v.setup()
            self.viewer = f"http://{v.host}:{v.fs_port}"
        self.reader = reader

    async def load(self, plate: str | None) -> None:
        for p in list(self.reader.children):
            self.reader.unassign_child_resource(p)
        if plate:
            self.reader.assign_child_resource(cor_96_wellplate_360uL_Fb("plate"))

    async def read_result(self, job: Job) -> dict:
        return reading(job)

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        if job.protocol not in self.PROTOCOLS:
            raise ValueError(f"unknown protocol {job.protocol!r}")
        plate = self.reader.get_plate()
        rows = "ABCDEFGHIJKLMNOP"[:plate.num_items_y]
        if off := [f"{w.row}{w.column}" for w in job.wells if w.row not in rows or w.column > plate.num_items_x]:
            raise ValueError(f"wells {', '.join(off)} aren't on a {plate.num_items}-well plate")
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


class PlrThermocyclerBackend:
    """`pcr`: run the plate through the PCR program (`PCR`): lid closed and heated,
    initial denaturation, the cycles, final extension, then a cold hold, lid open.
    A person's load puts the plate on the block, an unload takes it off. Cycling
    can't be undone, so the job crosses the stop boundary as the program starts:
    a safe stop before that leaves the plate as it was loaded."""

    PROTOCOLS = {"pcr"}

    def __init__(self, step: float = 0.5, visualize: bool = False):
        self.step = step  # simulated seconds per cycle
        self.visualize = visualize
        self.tc: Thermocycler | None = None
        self.viewer: str | None = None

    async def setup(self) -> None:
        tc = Thermocycler("thermocycler", size_x=127.76, size_y=85.48, size_z=100,
                          backend=ThermocyclerChatterboxBackend(), child_location=Coordinate.zero())
        await tc.setup()
        if self.visualize:
            v = Visualizer(resource=tc, open_browser=False)
            await v.setup()
            self.viewer = f"http://{v.host}:{v.fs_port}"
        self.tc = tc

    async def load(self, plate: str | None) -> None:
        for p in list(self.tc.children):
            self.tc.unassign_child_resource(p)
        if plate:
            self.tc.assign_child_resource(azenta_96_wellplate_200uL_Vb_4titudeframestar("plate"))

    async def read_result(self, job: Job) -> dict:
        return amplified(job)

    async def run(self, job: Job, progress: Progress) -> dict:
        tc = self.tc
        await asyncio.sleep(self.step)  # plate check
        progress()
        if job.protocol not in self.PROTOCOLS:
            raise ValueError(f"unknown protocol {job.protocol!r}")
        if not tc.children:
            raise RuntimeError("no plate on the block")
        plate = tc.children[0]
        rows = "ABCDEFGHIJKLMNOP"[:plate.num_items_y]
        if off := [f"{w.row}{w.column}" for w in job.wells if w.row not in rows or w.column > plate.num_items_x]:
            raise ValueError(f"wells {', '.join(off)} aren't on a {plate.num_items}-well plate")
        progress(state=State.running)
        await tc.close_lid()
        await asyncio.sleep(self.step)
        progress(stoppable=False)  # the program starts: the samples are changed from here on
        # The chatterbox runs the whole program at once; pace it one step per cycle, as a device would report.
        await tc.run_pcr_profile(
            denaturation_temp=[PCR["denaturation"][0]], denaturation_time=PCR["denaturation"][1],
            annealing_temp=[PCR["annealing"][0]], annealing_time=PCR["annealing"][1],
            extension_temp=[PCR["extension"][0]], extension_time=PCR["extension"][1],
            num_cycles=PCR["cycles"], block_max_volume=PCR["volume_ul"], lid_temperature=[PCR["lid"]],
            pre_denaturation_temp=[PCR["initial_denaturation"][0]], pre_denaturation_time=PCR["initial_denaturation"][1],
            final_extension_temp=[PCR["final_extension"][0]], final_extension_time=PCR["final_extension"][1],
            storage_temp=[PCR["hold"]], storage_time=0)
        for _ in range(PCR["cycles"]):
            await asyncio.sleep(self.step)
            progress()
        await tc.wait_for_profile_completion(poll_interval=self.step)
        await tc.deactivate_lid()
        await tc.open_lid()
        progress()
        return amplified(job)
