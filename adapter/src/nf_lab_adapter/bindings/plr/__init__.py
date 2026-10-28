"""PyLabRobot bindings on simulated (chatterbox) machines, one module per machine.

PyLabRobot is a library with no job service, so a binding runs the protocol in-process.
One simulated machine lives as long as the adapter. A person's load (`load(plate)`) sets
it up for that plate; an unload (`load(None)`) clears it. The labware carries no plate ID:
the adapter's ledger keeps which plate is on which machine.

With `visualize=True`, PyLabRobot's browser view of a machine starts with the adapter
(http://127.0.0.1:1337, or the next free port; the URL is logged). The pump array, powder
dispenser, sealer, peeler and barcode scanner have none: PyLabRobot doesn't model them as deck
resources.

Adding a capability: a module with a `PlrMachine` subclass (`PlrPlateHolder` for a machine that
holds one plate through a program; for a machine with no PyLabRobot simulator, a simulated
backend of our own, logging only, as in `sealer.py`), its row in `MACHINES`, its wet module,
and its rows in the conformance kit (`conformance/conftest.py`) and `wet-modules/tests/run.sh`.
"""

from pylabrobot.resources import set_volume_tracking

from .barcode_scanner import PlrBarcodeScannerBackend
from .centrifuge import PlrCentrifugeBackend
from .heater_shaker import PlrHeaterShakerBackend
from .incubator import PlrIncubatorBackend
from .liquid_handler import PlrLiquidHandlerBackend
from .plate_reader import PlrPlateReaderBackend
from .powder_dispenser import PlrPowderDispenserBackend
from .pump import PlrPumpBackend
from .scale import PlrScaleBackend
from .sealer import PlrPeelerBackend, PlrSealerBackend
from .shaker import PlrShakerBackend
from .temperature_controller import PlrTemperatureControllerBackend
from .thermocycler import PlrQpcrBackend, PlrThermocyclerBackend
from .tilter import PlrTilterBackend

set_volume_tracking(True)  # process-wide: wells track volume, so a transfer is checkable

MACHINES = {
    "liquid-handling": PlrLiquidHandlerBackend,
    "plate-reading": PlrPlateReaderBackend,
    "thermocycling": PlrThermocyclerBackend,
    "qpcr": PlrQpcrBackend,
    "shaking": PlrShakerBackend,
    "heating-shaking": PlrHeaterShakerBackend,
    "temperature-control": PlrTemperatureControllerBackend,
    "tilting": PlrTilterBackend,
    "centrifugation": PlrCentrifugeBackend,
    "incubation": PlrIncubatorBackend,
    "pumping": PlrPumpBackend,
    "weighing": PlrScaleBackend,
    "powder-dispensing": PlrPowderDispenserBackend,
    "sealing": PlrSealerBackend,
    "peeling": PlrPeelerBackend,
    "barcode-reading": PlrBarcodeScannerBackend,
}

__all__ = ["MACHINES", "PlrBarcodeScannerBackend", "PlrCentrifugeBackend", "PlrHeaterShakerBackend", "PlrIncubatorBackend",
           "PlrLiquidHandlerBackend", "PlrPeelerBackend", "PlrPlateReaderBackend", "PlrPowderDispenserBackend",
           "PlrPumpBackend", "PlrQpcrBackend", "PlrScaleBackend", "PlrSealerBackend", "PlrShakerBackend",
           "PlrTemperatureControllerBackend", "PlrThermocyclerBackend", "PlrTilterBackend"]
