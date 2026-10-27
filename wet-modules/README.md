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

| Subworkflow | What it does |
|---|---|
| `parallel_extraction` | `plate_split` → `rna_extraction` and `dna_extraction` side by side → `pcr` on each eluate plate |

**Tests.** One nf-test per module (`modules/<protocol>/tests/main.nf.test` and its `.snap`), as in nf-core/modules: run the process on a small plate, assert it succeeded and the provenance says `done`, snapshot the parsed `result.json`. The subworkflow's test is a `nextflow_pipeline` over a small typed script (`tests/main.nf`), because nf-test's generated workflow script is untyped and can't pass the typed channel the subworkflow needs; it snapshots the sorted names of the tasks that succeeded. Provenance and log hold device IDs and times, so they're only checked for what the module promises. No `-profile`: the module's tool is `labctl`, so no conda or container. The tests need a device adapter, so `tests/run.sh` starts a fake one on :8190 with a throwaway ledger, puts `labctl` on `PATH` and runs nf-test; its arguments go to `nf-test test`:

```bash
make install                      # once: the nf-lab plugin (SDKMAN Java 17)
(cd adapter && uv sync)           # once
export NXF_VER=26.10.2 NF_TEST=~/Desktop/Tools/nf-test
wet-modules/tests/run.sh                          # all seven (about 1 min)
wet-modules/tests/run.sh modules/pcr              # one module
wet-modules/tests/run.sh --tag pcr                # the module and the subworkflows that include it
wet-modules/tests/run.sh --update-snapshot        # after a result shape changes
```

Open: where `labctl` lives for a pipeline that includes these modules (a module `bin/` or the plugin); where the `Plate` record is declared once.
