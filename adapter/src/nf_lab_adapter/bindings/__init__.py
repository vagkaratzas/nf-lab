"""Backend bindings: start, report on and stop a job on one kind of device.

A binding is one async `run(job, progress)`:
- call `progress()` whenever the device reports (refreshes the heartbeat);
  `progress(state=State.running)` to change phase; `progress(stoppable=False)`
  just before the first liquid moves (the stop boundary). A binding that moves
  no liquid (a plate reader) never crosses it: its failures are retryable;
- return the job's outputs as a dict;
- raise to fail. The adapter turns the error into `failed_preflight` or
  `failed_in_protocol` from the stop boundary alone; raise `DeviceStopped` for
  a human stop;
- on cancellation, stop issuing commands and re-raise. It comes from safe_stop
  (always before the boundary) or from the device going silent (either side).
  Send the device a cancel only if the binding hasn't crossed the boundary yet;
  past it, never abort: a human checks the deck.

Two more calls come from a person (or the simulated operator), never from a job:
- `load(plate)`: the person put that plate on the deck and checked the deck;
  `load(None)`: they took it off. A simulation sets its deck up here.
- `read_result(job)`: the job's outputs as the deck shows them now. Used when a
  person resolves a failed job as done after checking the deck.
"""

import zlib
from typing import Protocol

from ..contract import Job, State, Well, WellType

ABSORBANCE_NM = 450


def absorbance(well: Well) -> float:
    """A simulated reading: controls and blanks near background, each sample its own value
    from its ID, so a re-read gives the same number."""
    if well.type != WellType.sample:
        return {WellType.negative_control: 0.05, WellType.blank: 0.04, WellType.positive_control: 1.8}[well.type]
    return round(0.2 + zlib.crc32(well.sample.encode()) % 1300 / 1000, 3)


def reading(job: Job) -> dict:
    """`plate_read`'s outputs: the absorbance of each plate-map well."""
    return {"plate": job.plate, "wavelength_nm": ABSORBANCE_NM,
            "wells": [{**w.model_dump(mode="json"), "absorbance": absorbance(w)} for w in job.wells]}


SPLIT_UL = 100.0  # `plate_split`: what each aliquot well gets
LYSIS_UL = 50.0  # extraction: lysis buffer added to each well
ELUATE_UL = 100.0  # extraction: lysate moved to the eluate (PCR) plate
EXTRACTIONS = {"rna_extraction": "RNA", "dna_extraction": "DNA"}  # protocol -> analyte


def aliquots(job: Job, volume=lambda aliquot, well: SPLIT_UL) -> dict:
    """`plate_split`'s outputs: two aliquot plates, each plate-map well in both. The
    pipeline names them; `volume(aliquot, well)` is what the well holds."""
    return {"plate": job.plate, "aliquots": [
        {"aliquot": i, "wells": [{**w.model_dump(mode="json"), "volume_ul": volume(i, w)} for w in job.wells]}
        for i in (1, 2)]}


def concentration(well: Well, analyte: str) -> float:
    """A simulated yield in ng/µL: none from controls and blanks, each sample its own
    value per analyte from its ID, so a re-extraction gives the same number."""
    if well.type == WellType.positive_control:
        return 50.0  # a known template
    if well.type != WellType.sample:
        return 0.0
    return round(5 + zlib.crc32(f"{well.sample}:{analyte}".encode()) % 9500 / 100, 1)


def extracted(job: Job, eluate=lambda well: ELUATE_UL) -> dict:
    """`rna_extraction`/`dna_extraction`'s outputs: each plate-map well's eluate and its yield."""
    analyte = EXTRACTIONS[job.protocol]
    return {"plate": job.plate, "analyte": analyte, "wells": [
        {**w.model_dump(mode="json"), "eluate_ul": eluate(w), "yield_ng_ul": concentration(w, analyte)}
        for w in job.wells]}


# `pcr`'s one program (°C, seconds). shortcut: fixed until jobs carry protocol parameters (DESIGN open question 12).
PCR = {"lid": 105.0, "initial_denaturation": [95.0, 180], "cycles": 30, "denaturation": [95.0, 15],
       "annealing": [60.0, 30], "extension": [72.0, 30], "final_extension": [72.0, 300], "hold": 4.0,
       "volume_ul": 25.0}


def amplified(job: Job) -> dict:
    """`pcr`'s outputs: the program the plate went through, and its plate-map wells."""
    return {"plate": job.plate, "program": PCR, "wells": [w.model_dump(mode="json") for w in job.wells]}


# `qpcr`'s one program: one-step RT-qPCR (reverse transcription, then PCR), fluorescence read
# each cycle at extension. shortcut: fixed until jobs carry protocol parameters (open question 12).
QPCR = {"lid": 105.0, "reverse_transcription": [50.0, 600], "initial_denaturation": [95.0, 120], "cycles": 40,
        "denaturation": [95.0, 15], "annealing": [60.0, 30], "extension": [72.0, 30], "final_extension": [72.0, 60],
        "hold": 4.0, "volume_ul": 20.0, "channel": "FAM"}
POSITIVE_CT = 25.0


def ct(well: Well) -> float | None:
    """A simulated threshold cycle: none (undetermined) for no-template controls and blanks;
    the positive control at `POSITIVE_CT`; each sample its own value from its ID, about one in
    seven not detected, so a re-run gives the same call."""
    if well.type == WellType.positive_control:
        return POSITIVE_CT
    h = zlib.crc32(f"{well.sample}:ct".encode())
    if well.type != WellType.sample or h % 7 == 0:
        return None
    return round(16 + h % 1800 / 100, 2)


def quantified(job: Job) -> dict:
    """`qpcr`'s outputs: the program, and each plate-map well's Ct (null: undetermined)."""
    return {"plate": job.plate, "program": QPCR, "wells": [{**w.model_dump(mode="json"), "ct": ct(w)} for w in job.wells]}


# Each plate-holder protocol's one program (°C, rpm, degrees, seconds): the plate sits on the machine
# and goes through it. shortcut: fixed until jobs carry protocol parameters (DESIGN open question 12).
PROGRAMS = {
    "plate_mix": {"speed_rpm": 1000, "duration_s": 60},  # shaking: mix reagent into each well
    "plate_lysis": {"temperature": 56.0, "speed_rpm": 1200, "duration_s": 600},  # heating-shaking: proteinase K lysis
    "heat_inactivation": {"temperature": 65.0, "duration_s": 1200},  # temperature-control: kill an enzyme
    "plate_tilt": {"angle_deg": 10.0, "duration_s": 120},  # tilting: pool each well's liquid to one side
    "plate_spin": {"g": 1000.0, "duration_s": 300},  # centrifugation: pellet what's in each well
    "plate_incubate": {"temperature": 37.0, "duration_s": 3600},  # incubation: grow or hold at 37 °C
}


def treated(job: Job) -> dict:
    """A plate-holder protocol's outputs: the program the plate went through, and its plate-map wells."""
    return {"plate": job.plate, "program": PROGRAMS[job.protocol], "wells": [w.model_dump(mode="json") for w in job.wells]}


# `reagent_dispense`, `powder_dispense`: what goes into each plate-map well.
# shortcut: fixed until jobs carry protocol parameters (open question 12).
DISPENSE = {"reagent": "wash buffer", "volume_ul": 50.0}
POWDER = {"powder": "NaCl", "amount_mg": 5.0}


def dispensed(job: Job) -> dict:
    """`reagent_dispense`'s outputs: the reagent, and the volume each plate-map well got."""
    return {"plate": job.plate, "reagent": DISPENSE["reagent"],
            "wells": [{**w.model_dump(mode="json"), "volume_ul": DISPENSE["volume_ul"]} for w in job.wells]}


def powdered(job: Job) -> dict:
    """`powder_dispense`'s outputs: the powder, and the amount each plate-map well got."""
    return {"plate": job.plate, "powder": POWDER["powder"],
            "wells": [{**w.model_dump(mode="json"), "amount_mg": POWDER["amount_mg"]} for w in job.wells]}


PLATE_TARE_G = 45.0  # an empty 96-well plate. shortcut: one plate type, until labware is in the job
WELL_UL = 150.0  # what each plate-map well holds when weighed (1 g/mL)


def weighed(job: Job) -> dict:
    """`plate_weigh`'s outputs: the plate's weight, the tare plus each plate-map well's liquid."""
    return {"plate": job.plate, "weight_g": round(PLATE_TARE_G + len(job.wells) * WELL_UL / 1000, 3),
            "wells": [w.model_dump(mode="json") for w in job.wells]}


# Each protocol's simulated outputs, from the job alone: what the fake binding returns, and a
# PyLabRobot binding's unless it reads them off its deck (liquid handling, the plate reader).
RESULTS = {"plate_read": reading, "plate_split": aliquots, **dict.fromkeys(EXTRACTIONS, extracted),
           "pcr": amplified, "qpcr": quantified, **dict.fromkeys(PROGRAMS, treated),
           "reagent_dispense": dispensed, "powder_dispense": powdered, "plate_weigh": weighed}
READ_ONLY = {"plate_read", "plate_weigh"}  # protocols that change nothing: they never cross the stop boundary


class DeviceStopped(Exception):
    """A human stopped the device (e-stop, vendor UI)."""


class Progress(Protocol):
    def __call__(self, *, state: State | None = None, stoppable: bool | None = None) -> None: ...


class Backend(Protocol):
    PROTOCOLS: set[str]  # the protocols this binding runs
    viewer: str | None  # URL of a live view of the deck, if the binding serves one

    async def setup(self) -> None: ...  # once, when the adapter starts
    async def run(self, job: Job, progress: Progress) -> dict: ...
    async def load(self, plate: str | None) -> None: ...
    async def read_result(self, job: Job) -> dict: ...
