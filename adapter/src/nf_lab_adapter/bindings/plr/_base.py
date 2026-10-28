"""What every PyLabRobot binding shares: pacing, the browser view, pre-flight checks and the plate."""

import asyncio

from pylabrobot.machines import Machine
from pylabrobot.resources import Resource, cor_96_wellplate_360uL_Fb
from pylabrobot.visualizer import Visualizer

from .. import RESULTS, Progress
from ...contract import Job, State

ROWS = "ABCDEFGHIJKLMNOP"


class PlrMachine:
    """A binding for one simulated PyLabRobot machine. Subclasses set `PROTOCOLS` and write
    `setup`, `load` and `run`; `read_result` defaults to the protocol's simulated result."""

    PROTOCOLS: set[str] = set()

    def __init__(self, step: float = 0.5, visualize: bool = False):
        self.step = step  # simulated seconds per operation: the chatterbox is instant, a machine isn't
        self.visualize = visualize
        self.viewer: str | None = None

    async def _serve(self, resource: Resource) -> None:
        """With `visualize`, serve PyLabRobot's browser view of the machine."""
        if self.visualize:
            v = Visualizer(resource=resource, open_browser=False)
            await v.setup()
            self.viewer = f"http://{v.host}:{v.fs_port}"  # the port it found free

    def _check(self, job: Job, plate) -> str:
        """Pre-flight: the protocol is ours and every plate-map well is on `plate`. Returns its row letters."""
        if job.protocol not in self.PROTOCOLS:
            raise ValueError(f"unknown protocol {job.protocol!r}")
        rows = ROWS[:plate.num_items_y]
        if off := [f"{w.row}{w.column}" for w in job.wells if w.row not in rows or w.column > plate.num_items_x]:
            raise ValueError(f"wells {', '.join(off)} aren't on a {plate.num_items}-well plate")
        return rows

    @staticmethod
    def _swap_plate(holder: Resource, plate: Resource | None) -> None:
        """A person's load puts `plate` on the holder; an unload (None) takes it off."""
        for p in list(holder.children):
            holder.unassign_child_resource(p)
        if plate is not None:
            holder.assign_child_resource(plate)

    async def read_result(self, job: Job) -> dict:
        return RESULTS[job.protocol](job)


class PlrPlateHolder(PlrMachine):
    """A machine that holds one plate and puts it through the job's program (`job.program`): a shaker, a
    heat block. A person's load puts a 96-well plate on it, an unload takes it off. The program
    changes the samples, so the job crosses the stop boundary as it starts: a safe stop before
    that leaves the plate as it was loaded. Subclasses write `make()` and `treat()`, and
    override `holder` when the plate sits on a part of the machine (a bucket, a tray)."""

    HOLD = 3  # simulated steps a program's hold takes

    def __init__(self, step: float = 0.5, visualize: bool = False):
        super().__init__(step, visualize)
        self.machine = None

    def make(self) -> Machine:
        raise NotImplementedError

    async def treat(self, program: dict, progress: Progress) -> None:
        raise NotImplementedError

    @property
    def holder(self) -> Resource:
        """Where a person's load puts the plate."""
        return self.machine

    async def ready(self, job: Job, progress: Progress) -> None:
        """Pre-flight after the plate checks, still stoppable: what the machine needs before the program."""
        await asyncio.sleep(self.step)

    async def setup(self) -> None:
        machine = self.make()
        await machine.setup()
        if isinstance(machine, Resource):  # a sealer or a peeler isn't a deck resource: no view
            await self._serve(machine)
        self.machine = machine

    async def load(self, plate: str | None) -> None:
        self._swap_plate(self.holder, cor_96_wellplate_360uL_Fb("plate") if plate else None)

    async def _hold(self, progress: Progress) -> None:
        """The program's hold, paced and reported as a device would."""
        for _ in range(self.HOLD):
            await asyncio.sleep(self.step)
            progress()

    async def run(self, job: Job, progress: Progress) -> dict:
        await asyncio.sleep(self.step)  # plate check
        progress()
        if not self.holder.children:
            raise RuntimeError(f"no plate on the {self.holder.name}")
        self._check(job, self.holder.children[0])
        progress(state=State.running)
        await self.ready(job, progress)
        progress(stoppable=False)  # the program starts: the samples are changed from here on
        await self.treat(job.program, progress)
        progress()
        return await self.read_result(job)
