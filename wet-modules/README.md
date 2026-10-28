# Wet modules

Nextflow modules for lab protocols, nf-core style. Nextflow only: no Python, no device code. A module doesn't know which robot runs it; a lab matches modules to its robots.

```
modules/<protocol>/
    main.nf               one process: `labctl run --protocol <protocol>`; hints 'lab/capability'
    meta.yml              nf-core meta.yml, plus `lab:` (capability, protocol, protocol_version, stop_boundary, params)
    input.schema.json     the protocol's params (names, types, units), set with `ext.params`
    result.schema.json    the result.json every binding of this protocol returns
subworkflows/<name>/{main.nf, meta.yml, tests/}
```

One module per protocol, named after it.

**Which modules can my equipment run?** Match each module's `meta.yml` `lab:` block against `GET /devices` (each robot's capability and protocols).

**What a binding must return.** `result.schema.json` is the join key with the bindings (the Python packages that drive a control stack). `adapter/tests/test_wet_modules.py` checks that `FakeBackend` and each PyLabRobot binding return a result that fits it, and that the module's `meta.yml`, script and schema agree on the protocol and capability.

**Setting a protocol's values.** Each module's `input.schema.json` lists what its protocol takes: `plate_spin` takes `g` and `duration_s`, `reagent_dispense` takes `reagent` and `volume_ul`, `pcr` its whole program (`cycles`, `annealing_temperature`, `annealing_s`, ...). Set them per process in the pipeline's config with `ext.params`; whatever you leave out runs at the lab's default:

```groovy
process {
    withName: 'PCR_SETUP:REAGENT_DISPENSE' {
        ext.params = [reagent: 'master mix', volume_ul: 20.0]
    }
    withName: 'PLATE_SPIN' {
        ext.params = [g: 500.0, duration_s: 120]
    }
}
```

The module passes the map to `labctl` as is, and the lab's adapter checks it: an unknown name, a wrong type or a value outside the lab's limits fails the task before the plate is touched (labctl exits 1, `refused: 422` and the reason in the task's error). Changed params are another physical job: they re-run the step, and the idempotency key changes with them. `provenance.json` holds both `params` (what you asked for) and `program` (what ran, defaults included). `examples/lab_tour/nextflow.config` sets three routines' reagents this way.

**Where the lab sets its limits.** Not in the pipeline: the defaults and limits live on the lab's side, in its device adapter, so a pipeline can't push a machine past what the lab allows. Two layers:

- **The machine's limits and defaults**, in the binding: `PARAMS` in `adapter/src/nf_lab_adapter/bindings/__init__.py` (for the simulated machines: the centrifuge's 10–4000 × g, the incubator's 4–50 °C, ...). A binding for real machines carries its own.
- **The lab's own**, in a TOML file the adapter reads at start (`NF_LAB_LIMITS=/path/limits.toml`): per protocol and param, `min`, `max`, `choices` (for strings), `default` and `fixed` (any other value is refused). It can only narrow the machine's limits; a value outside them, or a typo in a protocol or param name, stops the adapter at start. For example `[plate_spin.g]` `max = 500.0` for a smaller rotor, or `[plate_incubate.temperature]` `fixed = 37.0`. The format and an example are in `adapter/README.md` › Protocol parameters.

**Using a module.** `include { PLATE_READ } from '<path to>/wet-modules/modules/plate_read'`. The pipeline needs `labctl` on the task's `PATH` (the examples symlink it in their `bin/`) and `process_wet` config (the examples' `nextflow.config`).

| Module | Capability | Protocol | Stop boundary |
|---|---|---|---|
| `plate_transfer` | liquid-handling | `plate_transfer` | first aspirate |
| `plate_split` | liquid-handling | `plate_split` | first aspirate |
| `rna_extraction` | liquid-handling | `rna_extraction` | first aspirate |
| `dna_extraction` | liquid-handling | `dna_extraction` | first aspirate |
| `plate_read` | plate-reading | `plate_read` | none (moves no liquid) |
| `pcr` | thermocycling | `pcr` | start of cycling |
| `qpcr` | qpcr | `qpcr` | start of cycling |
| `plate_mix` | shaking | `plate_mix` | shaking starts |
| `plate_lysis` | heating-shaking | `plate_lysis` | heating starts |
| `heat_inactivation` | temperature-control | `heat_inactivation` | the block's temperature is set |
| `plate_tilt` | tilting | `plate_tilt` | the tilt starts |
| `plate_spin` | centrifugation | `plate_spin` | the spin starts |
| `plate_incubate` | incubation | `plate_incubate` | the plate is taken in (incubator preheated in pre-flight) |
| `reagent_dispense` | pumping | `reagent_dispense` | first pump |
| `powder_dispense` | powder-dispensing | `powder_dispense` | first dose |
| `plate_weigh` | weighing | `plate_weigh` | none (changes nothing) |
| `plate_seal` | sealing | `plate_seal` | the seal starts |
| `plate_unseal` | peeling | `plate_unseal` | the peel starts |
| `plate_scan` | barcode-reading | `plate_scan` | none (changes nothing; a barcode that isn't the job's plate fails it) |

| Subworkflow | What it does |
|---|---|
| `parallel_extraction` | `plate_split` → `rna_extraction` and `dna_extraction` side by side → `pcr` on each eluate plate |
| `rt_qpcr` | `rna_extraction` → `qpcr` on the eluate plate |
| `pcr_setup` | `plate_scan` → `reagent_dispense` (master mix) → `plate_transfer` (samples) → `plate_seal` → `plate_spin` → `pcr` → `plate_unseal` |
| `cell_viability` | `plate_scan` → `reagent_dispense` (viability reagent) → `plate_mix` → `plate_incubate` → `plate_read` |
| `dna_extraction_qc` | `plate_lysis` → `plate_spin` → `dna_extraction` → `plate_read` (yield per eluate well) |
| `compound_prep` | `powder_dispense` → `plate_weigh` (dose check) → `reagent_dispense` (solvent) → `plate_mix` |

**Tests.** nf-tests per module (`modules/<protocol>/tests/main.nf.test` and its `.snap`), as in nf-core/modules: run the process on a small plate, assert it succeeded and the provenance says `done`, snapshot the parsed `result.json`; then again with `ext.params` from the test's `params.config`, checking the program ran; `plate_spin`, `plate_weigh` and `plate_scan` also check that an unknown param (`refused.config`) fails the task. The subworkflow's test is a `nextflow_pipeline` over a small typed script (`tests/main.nf`), because nf-test's generated workflow script is untyped and can't pass the typed channel the subworkflow needs; it snapshots the sorted names of the tasks that succeeded. Provenance and log hold device IDs and times, so they're only checked for what the module promises. No `-profile`: the module's tool is `labctl`, so no conda or container. The tests need a device adapter, so `tests/run.sh` starts a fake one on :8190 with a throwaway ledger, puts `labctl` on `PATH` and runs nf-test; its arguments go to `nf-test test`:

```bash
make install                      # once: the nf-lab plugin (SDKMAN Java 17)
(cd adapter && uv sync)           # once
export NXF_VER=26.10.2 NF_TEST=~/Desktop/Tools/nf-test
wet-modules/tests/run.sh                          # every module and subworkflow (about 5 min)
wet-modules/tests/run.sh modules/pcr              # one module
wet-modules/tests/run.sh --tag pcr                # the module and the subworkflows that include it
wet-modules/tests/run.sh --update-snapshot        # after a result shape changes
```

Open: where `labctl` lives for a pipeline that includes these modules (a module `bin/` or the plugin); where the `Plate` record is declared once.
