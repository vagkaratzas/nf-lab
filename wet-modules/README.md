# Wet modules

Nextflow modules for lab protocols, nf-core style. Nextflow only: no Python, no device code. A module doesn't know which robot runs it; a lab matches modules to its robots.

```
modules/<protocol>/
    main.nf               one process: `labctl run --protocol <protocol>`; hints 'lab/capability'
    meta.yml              nf-core meta.yml, plus `lab:` (capability, protocol, protocol_version, stop_boundary)
    result.schema.json    the result.json every binding of this protocol returns
subworkflows/<name>/{main.nf, meta.yml, tests/}
```

One module per protocol, named after it.

**Which modules can my equipment run?** Match each module's `meta.yml` `lab:` block against `GET /devices` (each robot's capability and protocols).

**What a binding must return.** `result.schema.json` is the join key with the bindings (the Python packages that drive a control stack). `adapter/tests/test_wet_modules.py` checks that `FakeBackend` and each PyLabRobot binding return a result that fits it, and that the module's `meta.yml`, script and schema agree on the protocol and capability.

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

**Tests.** One nf-test per module (`modules/<protocol>/tests/main.nf.test` and its `.snap`), as in nf-core/modules: run the process on a small plate, assert it succeeded and the provenance says `done`, snapshot the parsed `result.json`. The subworkflow's test is a `nextflow_pipeline` over a small typed script (`tests/main.nf`), because nf-test's generated workflow script is untyped and can't pass the typed channel the subworkflow needs; it snapshots the sorted names of the tasks that succeeded. Provenance and log hold device IDs and times, so they're only checked for what the module promises. No `-profile`: the module's tool is `labctl`, so no conda or container. The tests need a device adapter, so `tests/run.sh` starts a fake one on :8190 with a throwaway ledger, puts `labctl` on `PATH` and runs nf-test; its arguments go to `nf-test test`:

```bash
make install                      # once: the nf-lab plugin (SDKMAN Java 17)
(cd adapter && uv sync)           # once
export NXF_VER=26.10.2 NF_TEST=~/Desktop/Tools/nf-test
wet-modules/tests/run.sh                          # all nine (about 1 min)
wet-modules/tests/run.sh modules/pcr              # one module
wet-modules/tests/run.sh --tag pcr                # the module and the subworkflows that include it
wet-modules/tests/run.sh --update-snapshot        # after a result shape changes
```

Open: where `labctl` lives for a pipeline that includes these modules (a module `bin/` or the plugin); where the `Plate` record is declared once.
