"""What every PyLabRobot binding shares: pacing, the browser view, pre-flight checks and the plate."""

from pylabrobot.resources import Resource
from pylabrobot.visualizer import Visualizer

from .. import RESULTS
from ...contract import Job

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
