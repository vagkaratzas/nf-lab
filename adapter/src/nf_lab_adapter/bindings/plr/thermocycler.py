"""`thermocycling` and `qpcr`: a simulated thermocycler (chatterbox backend), and a real-time PCR
instrument run on it."""

import asyncio

from pylabrobot.resources import Coordinate, azenta_96_wellplate_200uL_Vb_4titudeframestar
from pylabrobot.thermocycling import Thermocycler, ThermocyclerChatterboxBackend

from .. import PCR, QPCR, Progress
from ...contract import Job, State
from ._base import PlrMachine


class PlrThermocyclerBackend(PlrMachine):
    """`pcr`: run the plate through the PCR program (`PCR`): lid closed and heated,
    initial denaturation, the cycles, final extension, then a cold hold, lid open.
    A person's load puts the plate on the block, an unload takes it off. Cycling
    can't be undone, so the job crosses the stop boundary as the program starts:
    a safe stop before that leaves the plate as it was loaded."""

    PROTOCOLS = {"pcr"}
    PROGRAM = PCR
    CYCLE = 0.2  # simulated steps per cycle: 30 cycles in 6 steps, so a demo isn't all PCR

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)  # a cycle takes CYCLE of a step
        self.tc: Thermocycler | None = None

    async def setup(self) -> None:
        tc = Thermocycler("thermocycler", size_x=127.76, size_y=85.48, size_z=100,
                          backend=ThermocyclerChatterboxBackend(), child_location=Coordinate.zero())
        await tc.setup()
        await self._serve(tc)
        self.tc = tc

    async def load(self, plate: str | None) -> None:
        self._swap_plate(self.tc, azenta_96_wellplate_200uL_Vb_4titudeframestar("plate") if plate else None)

    async def run(self, job: Job, progress: Progress) -> dict:
        tc, PCR = self.tc, self.PROGRAM
        await asyncio.sleep(self.step)  # plate check
        progress()
        if not tc.children:
            raise RuntimeError("no plate on the block")
        self._check(job, tc.children[0])
        progress(state=State.running)
        await tc.close_lid()
        await asyncio.sleep(self.step)
        progress(stoppable=False)  # the program starts: the samples are changed from here on
        if rt := PCR.get("reverse_transcription"):  # RT-qPCR: RNA to cDNA on the block first
            await tc.set_block_temperature([rt[0]])
            await asyncio.sleep(self.step)
            progress()
        # The chatterbox runs the whole program at once; pace it, reporting each cycle as a device would.
        await tc.run_pcr_profile(
            denaturation_temp=[PCR["denaturation"][0]], denaturation_time=PCR["denaturation"][1],
            annealing_temp=[PCR["annealing"][0]], annealing_time=PCR["annealing"][1],
            extension_temp=[PCR["extension"][0]], extension_time=PCR["extension"][1],
            num_cycles=PCR["cycles"], block_max_volume=PCR["volume_ul"], lid_temperature=[PCR["lid"]],
            pre_denaturation_temp=[PCR["initial_denaturation"][0]], pre_denaturation_time=PCR["initial_denaturation"][1],
            final_extension_temp=[PCR["final_extension"][0]], final_extension_time=PCR["final_extension"][1],
            storage_temp=[PCR["hold"]], storage_time=0)
        for _ in range(PCR["cycles"]):
            await asyncio.sleep(self.step * self.CYCLE)
            progress()
        await tc.wait_for_profile_completion(poll_interval=self.step)
        await tc.deactivate_lid()
        await tc.open_lid()
        progress()
        return await self.read_result(job)


class PlrQpcrBackend(PlrThermocyclerBackend):
    """`qpcr`: a real-time PCR instrument, a thermocycler that reads fluorescence every cycle.
    One-step RT-qPCR (`QPCR`): reverse transcription, then the cycles; each plate-map well's
    threshold cycle (Ct) in the result. PyLabRobot 0.2.2 simulates no qPCR instrument, so this
    runs the program on its simulated thermocycler and simulates the reads (`ct`)."""

    PROTOCOLS = {"qpcr"}
    PROGRAM = QPCR
    # A read every cycle: a plate takes longer than its extraction (~1.5 h against ~1 h in a lab),
    # so plates pile up at the qPCR pool, as they would.
    CYCLE = 3.0
