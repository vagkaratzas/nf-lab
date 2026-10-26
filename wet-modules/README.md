# Wet modules

Nextflow modules for lab protocols, nf-core style. Nextflow only: no Python, no device code. A module doesn't know which robot runs it; a lab matches modules to its robots.

```
modules/<protocol>/
    main.nf               one process: `labctl run --protocol <protocol>`; hints 'lab/capability'
    meta.yml              nf-core meta.yml, plus `lab:` (capability, protocol, protocol_version, stop_boundary)
    result.schema.json    the result.json every binding of this protocol returns
subworkflows/<name>/main.nf
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

| Subworkflow | What it does |
|---|---|
| `parallel_extraction` | `plate_split` → `rna_extraction` and `dna_extraction` side by side → `pcr` on each eluate plate |

Open: where `labctl` lives for a pipeline that includes these modules (a module `bin/` or the plugin); where the `Plate` record is declared once; subworkflow `meta.yml`.
