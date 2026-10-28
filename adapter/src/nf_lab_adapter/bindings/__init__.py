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

import math
import re
import tomllib
import zlib
from dataclasses import dataclass
from typing import Protocol

from ..contract import Job, State, Well, WellType


@dataclass(frozen=True)
class Param:
    """One protocol parameter: its type, the binding's default and the machine's limits."""

    type: type  # float, int or str
    default: float | int | str
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None


# A string param must hold one character outside this class. Explicit, so Python's `re` and the
# ECMAScript regex of JSON Schema (each module's input.schema.json `pattern`) read it the same.
NONBLANK = r"[^\t\n\v\f\r ]"

C = {"min": 4.0, "max": 99.0}  # a block temperature, °C
S = {"min": 1, "max": 3600}  # a cycling step, seconds
PCR_PARAMS = {
    "lid_temperature": Param(float, 105.0, 30.0, 110.0),
    "initial_denaturation_temperature": Param(float, 95.0, **C), "initial_denaturation_s": Param(int, 180, **S),
    "cycles": Param(int, 30, 1, 60),
    "denaturation_temperature": Param(float, 95.0, **C), "denaturation_s": Param(int, 15, **S),
    "annealing_temperature": Param(float, 60.0, **C), "annealing_s": Param(int, 30, **S),
    "extension_temperature": Param(float, 72.0, **C), "extension_s": Param(int, 30, **S),
    "final_extension_temperature": Param(float, 72.0, **C), "final_extension_s": Param(int, 300, **S),
    "hold_temperature": Param(float, 4.0, **C),
    "volume_ul": Param(float, 25.0, 1.0, 100.0),
}

# Each protocol's parameters: what its module's input.schema.json lists, with the binding's defaults
# and the simulated machines' limits. The maxima are chosen so any combination fits the simulated deck
# (a split draws 2 x volume_ul from a 300 µL source well; an eluate goes into a 200 µL PCR well).
# A lab narrows them, fixes values or sets its own defaults in its limits file (`load_limits`).
# shortcut: one spec for both bindings, since both simulate the same machines; a binding for other
# machines (SiLA 2) brings its own when one is written.
PARAMS: dict[str, dict[str, Param]] = {
    "plate_transfer": {"volume_ul": Param(float, 150.0, 1.0, 300.0)},
    "plate_split": {"volume_ul": Param(float, 100.0, 1.0, 150.0)},  # into each aliquot
    **{p: {"lysis_ul": Param(float, 50.0, 1.0, 60.0), "eluate_ul": Param(float, 100.0, 1.0, 200.0)}
       for p in ("rna_extraction", "dna_extraction")},
    "plate_read": {"wavelength_nm": Param(int, 450, 230, 1000)},
    "pcr": PCR_PARAMS,
    "qpcr": {**PCR_PARAMS, "cycles": Param(int, 40, 1, 60), "initial_denaturation_s": Param(int, 120, **S),
             "final_extension_s": Param(int, 60, **S), "volume_ul": Param(float, 20.0, 1.0, 100.0),
             "reverse_transcription_temperature": Param(float, 50.0, **C), "reverse_transcription_s": Param(int, 600, **S),
             "channel": Param(str, "FAM", choices=("FAM", "HEX", "ROX", "Cy5"))},
    "plate_mix": {"speed_rpm": Param(int, 1000, 100, 3000), "duration_s": Param(int, 60, 1, 3600)},
    "plate_lysis": {"temperature": Param(float, 56.0, 20.0, 95.0), "speed_rpm": Param(int, 1200, 100, 2000),
                    "duration_s": Param(int, 600, 1, 7200)},
    "heat_inactivation": {"temperature": Param(float, 65.0, **C), "duration_s": Param(int, 1200, 1, 7200)},
    "plate_tilt": {"angle_deg": Param(float, 10.0, 0.0, 20.0), "duration_s": Param(int, 120, 1, 3600)},
    "plate_spin": {"g": Param(float, 1000.0, 10.0, 4000.0), "duration_s": Param(int, 300, 1, 3600)},
    "plate_incubate": {"temperature": Param(float, 37.0, 4.0, 50.0), "duration_s": Param(int, 3600, 1, 604800)},
    "plate_seal": {"temperature": Param(int, 165, 100, 200), "duration_s": Param(float, 1.5, 0.5, 10.0)},
    "plate_unseal": {"adhere_time_s": Param(float, 2.5, 0.5, 10.0)},
    "reagent_dispense": {"reagent": Param(str, "wash buffer"), "volume_ul": Param(float, 50.0, 1.0, 300.0)},
    "powder_dispense": {"powder": Param(str, "NaCl"), "amount_mg": Param(float, 5.0, 0.1, 100.0)},
    "plate_weigh": {},
    "plate_scan": {},
}
LIMIT_KEYS = {"min", "max", "choices", "default", "fixed"}


def _value(where: str, p: Param, value, rule: dict | None = None):
    """`value` as `p` takes it, within the machine's limits and the lab's `rule`; ValueError if not."""
    rule = rule or {}
    if p.type is str:
        if not isinstance(value, str) or not re.search(NONBLANK, value):
            raise ValueError(f"{where} must be a non-blank string, got {value!r}")
        if (choices := rule.get("choices") or p.choices) and value not in choices:
            raise ValueError(f"{where} must be one of {', '.join(choices)}, got {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, int if p.type is int else (int, float)):
        raise ValueError(f"{where} must be {'an integer' if p.type is int else 'a number'}, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{where} must be finite, got {value!r}")
    lo, hi = max(p.min, rule.get("min", p.min)), min(p.max, rule.get("max", p.max))
    if not lo <= value <= hi:
        raise ValueError(f"{where} must be within {lo}–{hi}, got {value!r}")
    return p.type(value)


def resolve(protocol: str, params: dict, limits: dict | None = None) -> dict:
    """The program a job runs: the binding's defaults, then the lab's (`limits`, from `load_limits`),
    then the params asked for. ValueError (the adapter's 422) for an unknown param, a wrong type,
    a value out of the machine's or the lab's limits, or one other than the lab fixed."""
    spec, lab = PARAMS.get(protocol, {}), (limits or {}).get(protocol, {})
    if unknown := sorted(set(params) - set(spec)):
        takes = f"it takes {', '.join(spec)}" if spec else "it takes none"
        raise ValueError(f"{protocol} has no param {', '.join(unknown)}: {takes}")
    program = {}
    for name, p in spec.items():
        rule = lab.get(name, {})
        if name not in params:
            program[name] = rule.get("fixed", rule.get("default", p.default))
            continue
        program[name] = _value(f"{protocol}.{name}", p, params[name], rule)
        if "fixed" in rule and program[name] != rule["fixed"]:
            raise ValueError(f"{protocol}.{name} is fixed at {rule['fixed']!r} by the lab, got {params[name]!r}")
    return program


def load_limits(path: str) -> dict:
    """A lab's limits file (TOML; `NF_LAB_LIMITS`), per protocol and param: `min`, `max` (numbers),
    `choices` (strings), `default`, `fixed` (any other value is refused). Every value must fit the
    machine's limits. ValueError for anything else, so the adapter doesn't start on a bad file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    limits: dict[str, dict[str, dict]] = {}
    for protocol, rules in raw.items():
        if protocol not in PARAMS or not isinstance(rules, dict):
            raise ValueError(f"{path}: {protocol!r} isn't a protocol table ([{protocol}.<param>])")
        for name, rule in rules.items():
            where = f"{path}: {protocol}.{name}"
            if name not in PARAMS[protocol] or not isinstance(rule, dict):
                raise ValueError(f"{where}: {protocol} has no param {name!r} (it takes {', '.join(PARAMS[protocol]) or 'none'})")
            if bad := sorted(set(rule) - LIMIT_KEYS):
                raise ValueError(f"{where}: unknown key {', '.join(bad)} (allowed: {', '.join(sorted(LIMIT_KEYS))})")
            p, out = PARAMS[protocol][name], {}
            if p.type is str:
                if {"min", "max"} & set(rule):
                    raise ValueError(f"{where}: min and max are for numbers; a string takes choices")
                if "choices" in rule:
                    if not isinstance(rule["choices"], list) or not rule["choices"]:
                        raise ValueError(f"{where}.choices must be a non-empty list")
                    out["choices"] = tuple(_value(f"{where}.choices", p, c) for c in rule["choices"])
            else:
                if "choices" in rule:
                    raise ValueError(f"{where}: choices are for strings; a number takes min and max")
                out |= {k: _value(f"{where}.{k}", p, rule[k]) for k in ("min", "max") if k in rule}
                if out.get("min", p.min) > out.get("max", p.max):
                    raise ValueError(f"{where}: min {out['min']} is above max {out['max']}")
            out |= {k: _value(f"{where}.{k}", p, rule[k], out) for k in ("default", "fixed") if k in rule}
            if "default" in out and "fixed" in out and out["default"] != out["fixed"]:
                raise ValueError(f"{where}: default and fixed differ; fixed is the default")
            limits.setdefault(protocol, {})[name] = out
    return limits


def absorbance(well: Well) -> float:
    """A simulated reading: controls and blanks near background, each sample its own value
    from its ID, so a re-read gives the same number."""
    if well.type != WellType.sample:
        return {WellType.negative_control: 0.05, WellType.blank: 0.04, WellType.positive_control: 1.8}[well.type]
    return round(0.2 + zlib.crc32(well.sample.encode()) % 1300 / 1000, 3)


def reading(job: Job) -> dict:
    """`plate_read`'s outputs: the absorbance of each plate-map well, at the program's wavelength."""
    return {"plate": job.plate, "wavelength_nm": job.program["wavelength_nm"],
            "wells": [{**w.model_dump(mode="json"), "absorbance": absorbance(w)} for w in job.wells]}


EXTRACTIONS = {"rna_extraction": "RNA", "dna_extraction": "DNA"}  # protocol -> analyte


def aliquots(job: Job, volume=None) -> dict:
    """`plate_split`'s outputs: two aliquot plates, each plate-map well in both. The
    pipeline names them; `volume(aliquot, well)` is what the well holds (default: the program's)."""
    volume = volume or (lambda aliquot, well: job.program["volume_ul"])
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


def extracted(job: Job, eluate=None) -> dict:
    """`rna_extraction`/`dna_extraction`'s outputs: each plate-map well's eluate (default: the
    program's `eluate_ul`) and its yield."""
    eluate = eluate or (lambda well: job.program["eluate_ul"])
    analyte = EXTRACTIONS[job.protocol]
    return {"plate": job.plate, "analyte": analyte, "wells": [
        {**w.model_dump(mode="json"), "eluate_ul": eluate(w), "yield_ng_ul": concentration(w, analyte)}
        for w in job.wells]}


def amplified(job: Job) -> dict:
    """`pcr`'s outputs: the program the plate went through, and its plate-map wells."""
    return {"plate": job.plate, "program": job.program, "wells": [w.model_dump(mode="json") for w in job.wells]}


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
    return {"plate": job.plate, "program": job.program, "wells": [{**w.model_dump(mode="json"), "ct": ct(w)} for w in job.wells]}


# The plate-holder protocols: the plate sits on the machine and goes through the program.
HOLDERS = ("plate_mix", "plate_lysis", "heat_inactivation", "plate_tilt", "plate_spin", "plate_incubate",
           "plate_seal", "plate_unseal")


def treated(job: Job) -> dict:
    """A plate-holder protocol's outputs: the program the plate went through, and its plate-map wells."""
    return {"plate": job.plate, "program": job.program, "wells": [w.model_dump(mode="json") for w in job.wells]}


def dispensed(job: Job) -> dict:
    """`reagent_dispense`'s outputs: the reagent, and the volume each plate-map well got."""
    return {"plate": job.plate, "reagent": job.program["reagent"],
            "wells": [{**w.model_dump(mode="json"), "volume_ul": job.program["volume_ul"]} for w in job.wells]}


def powdered(job: Job) -> dict:
    """`powder_dispense`'s outputs: the powder, and the amount each plate-map well got."""
    return {"plate": job.plate, "powder": job.program["powder"],
            "wells": [{**w.model_dump(mode="json"), "amount_mg": job.program["amount_mg"]} for w in job.wells]}


PLATE_TARE_G = 45.0  # an empty 96-well plate. shortcut: one plate type, until labware is in the job
WELL_UL = 150.0  # what each plate-map well holds when weighed (1 g/mL)


def weighed(job: Job) -> dict:
    """`plate_weigh`'s outputs: the plate's weight, the tare plus each plate-map well's liquid."""
    return {"plate": job.plate, "weight_g": round(PLATE_TARE_G + len(job.wells) * WELL_UL / 1000, 3),
            "wells": [w.model_dump(mode="json") for w in job.wells]}


def scanned(job: Job) -> dict:
    """`plate_scan`'s outputs: the barcode read off the plate (the job's plate ID), and its plate-map wells."""
    return {"plate": job.plate, "barcode": job.plate, "wells": [w.model_dump(mode="json") for w in job.wells]}


# Each protocol's simulated outputs, from the job alone: what the fake binding returns, and a
# PyLabRobot binding's unless it reads them off its deck (liquid handling, the plate reader).
RESULTS = {"plate_read": reading, "plate_split": aliquots, **dict.fromkeys(EXTRACTIONS, extracted),
           "pcr": amplified, "qpcr": quantified, **dict.fromkeys(HOLDERS, treated),
           "reagent_dispense": dispensed, "powder_dispense": powdered, "plate_weigh": weighed,
           "plate_scan": scanned}
READ_ONLY = {"plate_read", "plate_weigh", "plate_scan"}  # protocols that change nothing: they never cross the stop boundary


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
