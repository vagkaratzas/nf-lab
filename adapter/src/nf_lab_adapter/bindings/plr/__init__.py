"""PyLabRobot bindings on simulated (chatterbox) machines, one module per machine.

PyLabRobot is a library with no job service, so a binding runs the protocol in-process.
One simulated machine lives as long as the adapter. A person's load (`load(plate)`) sets
it up for that plate; an unload (`load(None)`) clears it. The labware carries no plate ID:
the adapter's ledger keeps which plate is on which machine.

With `visualize=True`, PyLabRobot's browser view of a machine starts with the adapter
(http://127.0.0.1:1337, or the next free port; the URL is logged).

Adding a capability: a module with a `PlrMachine` subclass, and its row in `MACHINES`.
"""

from pylabrobot.resources import set_volume_tracking

from .liquid_handler import PlrLiquidHandlerBackend
from .plate_reader import PlrPlateReaderBackend
from .thermocycler import PlrQpcrBackend, PlrThermocyclerBackend

set_volume_tracking(True)  # process-wide: wells track volume, so a transfer is checkable

MACHINES = {
    "liquid-handling": PlrLiquidHandlerBackend,
    "plate-reading": PlrPlateReaderBackend,
    "thermocycling": PlrThermocyclerBackend,
    "qpcr": PlrQpcrBackend,
}

__all__ = ["MACHINES", "PlrLiquidHandlerBackend", "PlrPlateReaderBackend", "PlrQpcrBackend", "PlrThermocyclerBackend"]
