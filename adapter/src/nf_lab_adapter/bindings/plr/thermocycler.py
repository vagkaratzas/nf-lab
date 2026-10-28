"""`thermocycling` and `qpcr`: a simulated thermocycler (chatterbox backend), and a real-time PCR
instrument run on it."""

import asyncio

from pylabrobot.resources import Coordinate, azenta_96_wellplate_200uL_Vb_4titudeframestar
from pylabrobot.thermocycling import Thermocycler, ThermocyclerChatterboxBackend

from .. import Progress
from ...contract import Job, State
from ._base import PlrMachine


class PlrThermocyclerBackend(PlrMachine):
    """`pcr`: run the plate through the job's PCR program (`job.program`): lid closed and heated,
    initial denaturation, the cycles, final extension, then a cold hold, lid open.
    A person's load puts the plate on the block, an unload takes it off. Cycling
    can't be undone, so the job crosses the stop boundary as the program starts:
    a safe stop before that leaves the plate as it was loaded."""

    PROTOCOLS = {"pcr"}
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
        tc, p = self.tc, job.program
        await asyncio.sleep(self.step)  # plate check
        progress()
        if not tc.children:
            raise RuntimeError("no plate on the block")
        self._check(job, tc.children[0])
        progress(state=State.running)
        await tc.close_lid()
        await asyncio.sleep(self.step)
        progress(stoppable=False)  # the program starts: the samples are changed from here on
        if "reverse_transcription_temperature" in p:  # RT-qPCR: RNA to cDNA on the block first
            await tc.set_block_temperature([p["reverse_transcription_temperature"]])
            await asyncio.sleep(self.step)
            progress()
        # The chatterbox runs the whole program at once; pace it, reporting each cycle as a device would.
        await tc.run_pcr_profile(
            denaturation_temp=[p["denaturation_temperature"]], denaturation_time=p["denaturation_s"],
            annealing_temp=[p["annealing_temperature"]], annealing_time=p["annealing_s"],
            extension_temp=[p["extension_temperature"]], extension_time=p["extension_s"],
            num_cycles=p["cycles"], block_max_volume=p["volume_ul"], lid_temperature=[p["lid_temperature"]],
            pre_denaturation_temp=[p["initial_denaturation_temperature"]], pre_denaturation_time=p["initial_denaturation_s"],
            final_extension_temp=[p["final_extension_temperature"]], final_extension_time=p["final_extension_s"],
            storage_temp=[p["hold_temperature"]], storage_time=0)
        for _ in range(p["cycles"]):
            await asyncio.sleep(self.step * self.CYCLE)
            progress()
        await tc.wait_for_profile_completion(poll_interval=self.step)
        await tc.deactivate_lid()
        await tc.open_lid()
        progress()
        return await self.read_result(job)


class PlrQpcrBackend(PlrThermocyclerBackend):
    """`qpcr`: a real-time PCR instrument, a thermocycler that reads fluorescence every cycle.
    One-step RT-qPCR (its program has `reverse_transcription_*`): reverse transcription, then the cycles; each plate-map well's
    threshold cycle (Ct) in the result. PyLabRobot 0.2.2 simulates no qPCR instrument, so this
    runs the program on its simulated thermocycler and simulates the reads (`ct`)."""

    PROTOCOLS = {"qpcr"}
    # A read every cycle: a plate takes longer than its extraction (~1.5 h against ~1 h in a lab),
    # so plates pile up at the qPCR pool, as they would.
    CYCLE = 3.0
