# Changelog

## [0.1.0] - 2026-10-30

First release: a prototype, built against PyLabRobot's simulated machines. Needs Nextflow 26.10.0 or later and Java 17.

### Added

- The `labrobotics` executor: a wet step never runs twice, not on retry, `-resume` or a new launch directory, and aborting a run safe-stops the robot instead of killing it mid-protocol. Each wet task's provenance (device, protocol version, samples) goes into Nextflow lineage.
- The `labrobotics {}` config scope (adapter URL, polling) and the `labPlate` function.
- The device adapter (`adapter/`): job ledger, robot pools and queues, a deck page for people at the bench, bearer-token auth (`NF_LAB_TOKEN`), lab limits for protocol parameters (`NF_LAB_LIMITS`), and bindings for every PyLabRobot 0.2.2 simulated machine. `labctl` is the client a wet task runs; `openapi.json` is the contract; the conformance kit checks any adapter against it.
- Wet modules (`wet-modules/`): 19 protocols, from `plate_transfer` and `rna_extraction` to `pcr` and `qpcr`, and 6 subworkflows that chain them, each with nf-tests.
- Example pipelines (`examples/`), including a 1000-sample RT-qPCR run, and a dashboard (`dashboard/`) with a live view of a run and scripted demos.
