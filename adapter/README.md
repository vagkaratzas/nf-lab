# nf-lab device adapter

The adapter stands between Nextflow and the lab's robots. A wet task in a pipeline doesn't talk to a robot. It runs `labctl`, which asks the adapter for a job ("transfer plate P1 on any liquid handler"). The adapter keeps every job in a ledger, so the same job never runs twice: not after a retry, not after `-resume`, not from a new launch directory. It runs each job on a robot that is free and holds the job's plate.

People at the bench use the adapter's **deck page** to load and unload plates and to deal with failures.

## Start it

```bash
cd adapter
uv sync
NF_LAB_BACKEND=plr NF_LAB_VISUALIZER=1 NF_LAB_DEVICES=2 uv run uvicorn nf_lab_adapter.app:main --factory
```

Then open:

- **http://127.0.0.1:8000/deck**: the deck page, one row per robot.
- **http://127.0.0.1:8000/docs**: every endpoint, with a "Try it out" button.

Settings (environment variables):

| Variable | Default | What it does |
|---|---|---|
| `NF_LAB_BACKEND` | `fake` | `plr`: PyLabRobot's simulated machines, one per capability in `bindings.plr.MACHINES` (a `plate-reading` robot is a plate reader, a `thermocycling` one a thermocycler, a `qpcr` one a real-time PCR instrument, `centrifugation` a centrifuge, …; any capability not listed there a liquid handler). `fake`: a scripted device for tests |
| `NF_LAB_DEVICES` | `1` | How many robots, all with the `liquid-handling` capability (`plr-sim-1`, `plr-sim-2`… or `fake-1`…); or pools as `capability[/queue]:count,…`, e.g. `liquid-handling/opentrons:2,liquid-handling/hamilton:1` (`opentrons-1`, `opentrons-2`, `hamilton-1`). A job naming a queue (`labctl --queue`, from Nextflow's `queue` directive) runs only on that queue's robots |
| `NF_LAB_VISUALIZER` | off | `1`: each PyLabRobot robot serves a live view of its deck (port 1337, then 1338…), linked from its row on the deck page |
| `NF_LAB_SIMULATED_OPERATOR` | on for `fake`, off for `plr` | `1`: nobody at the bench is needed, because the adapter loads and unloads plates itself. Failed jobs still wait for a person |
| `NF_LAB_LEDGER` | `ledger.sqlite` | The ledger file. Keep it between runs: it is what stops a job from running twice |
| `NF_LAB_STEP` | `1.0` | Seconds per simulated robot step |
| `NF_LAB_SILENT_AFTER` | `600` | Seconds without a report from a robot before its job fails as `device_silent` |
| `NF_LAB_TOKEN` | unset | Every request must carry it as `Authorization: Bearer <token>`, the deck page's buttons included; unset: no check. See [The token](#the-token) |
| `NF_LAB_LIMITS` | unset | The lab's limits file for protocol parameters (TOML), read at start; unset: the machines' own limits. See [Protocol parameters](#protocol-parameters) |

## Three questions the adapter answers

### What can each robot do?

The deck page lists each robot's protocols under its name. For the same list without the page:

```bash
curl -s localhost:8000/devices | jq '.[] | {device_id, capability, protocols}'
```

A job asks for a **capability** (`liquid-handling`), not a robot. The PyLabRobot liquid handler runs `plate_transfer`: it fills each listed well of the destination plate from the same well of the sample plate (controls and blanks: from the buffer plate). It also runs `plate_split` (the same, 100 µL into each of two aliquot plates, which the pipeline names) and `rna_extraction` / `dna_extraction` (on an aliquot: 50 µL lysis buffer into each plate-map well, then 100 µL of lysate to a 96-well PCR plate (200 µL wells), the plate the thermocycler takes; each well's `eluate_ul` and a simulated `yield_ng_ul`). The plate reader runs `plate_read` (absorbance at 450 nm), and the thermocycler `pcr`: its PCR program, paced a fifth of `NF_LAB_STEP` per cycle, past the stop boundary once cycling starts. The real-time PCR instrument runs `qpcr`: one-step RT-qPCR (reverse transcription, then 40 cycles) on the same simulated thermocycler, with a simulated fluorescence read giving each well a threshold cycle `ct` (null: undetermined; PyLabRobot simulates no qPCR instrument). Plate holders put the plate through their program, past the stop boundary as it starts, and return it in `result.program`: `plate_mix` (shaker), `plate_lysis` (heater-shaker), `heat_inactivation` (heat block; PyLabRobot's simulated one has no active cooling and reaches its target at once), `plate_tilt` (tilter), `plate_spin` (centrifuge, a balance plate in the other bucket) and `plate_incubate` (incubator: pre-flight heats it to the program's temperature, 37 °C by default, and picks a free rack site, then the boundary is taking the plate in off the loading tray, which it comes back to). The pump array runs `reagent_dispense` (50 µL into each plate-map well, one pump per plate column, read back from the plate), the powder dispenser `powder_dispense` (each well's `amount_mg` as the dispenser reports it) and the balance `plate_weigh` (read only, like `plate_read`: `weight_g`, the tare plus the plate-map liquid). PyLabRobot 0.2.2 has drivers but no simulator for a sealer, a peeler or a barcode scanner, so these run on simulated backends of our own (PyLabRobot's backend interface, logging only): `plate_seal` and `plate_unseal` (plate holders; the boundary is the seal or the peel) and `plate_scan` (read only: the barcode must be the job's plate ID, else the job fails before anything happens). Plate maps take a `positive_control` well type besides `negative_control` and `blank`. The fake backend runs these protocols too, instantly and with the same result shape, so `examples/plate_transfer` runs without PyLabRobot; its other protocols (`ok`, `preflight_fail`, `protocol_fail`…) are scripted failures for tests. Every volume, temperature, time and reagent above is the protocol's default: a job's params change it (next section).

### Which robots are free, and which are ready?

Free is not ready. A **free** robot has no job. A **ready** robot also has the right plate on its deck, and a person has checked the deck. Each row of the deck page shows a status:

| Status | Meaning | What to do |
|---|---|---|
| `empty` | Nothing on the deck | Load a plate |
| `ready` | A plate is on the deck; its queued jobs start here | Nothing |
| `running` | A job is running | Wait, or press E-stop |
| `unload` | The job is done; the plate is still on the deck | Take the plate off, press Unload |
| `needs_person` | The job failed after liquid moved, or someone stopped it | Check the deck, press Succeeded or Failed, then Unload |

The line under the status says the same in words. It also names the last job's **pre-flight failure** (a failure before any liquid moved), which frees the robot by itself:

```bash
curl -s localhost:8000/devices | jq '.[] | {device_id, status, detail}'
```

### Why isn't my job running?

The Queue section at the top of the deck page lists every waiting job with what it is waiting for, for example:

- `plate P4 isn't on any deck: load it on plr-sim-2`
- `plate P1 is on plr-sim-1, done with plate P1: unload it, then load P1 again`

```bash
curl -s localhost:8000/queue | jq '.[] | {plate, protocol, waiting_for}'
```

The plate box on each free robot's row suggests the next plate to load.

## A plate's life

1. **Queued.** A pipeline (or you, see below) submits a job for a plate. It waits in one queue shared by all robots with that capability.
2. **Load.** A person puts the plate on a robot and presses *Load plate + confirm deck*. A plate is on one robot at most. Load is refused while the robot still holds a job.
3. **Runs.** The robot takes the oldest queued job for the plate on its deck, one job at a time. Until the first liquid moves, the job can still be stopped safely and retried. After that it only runs to the end.
4. **Unload.** When the job is done the plate stays on the deck, holding the robot, until a person presses *Unload*. Nothing else starts on that robot before then.

A job that fails after liquid moved, or is e-stopped, keeps the robot until a person deals with it: see the next section.

## When a job fails

A job that fails **before** any liquid moved (`failed_preflight`) needs nobody: nothing was used, the robot is free again, and the pipeline retries it (exit `75`).

A job that fails **after** liquid moved (`failed_in_protocol`), or that someone e-stopped (`stopped`), is never retried by itself. The robot stays held, the pipeline run ends (exit `1`; the other robots finish what they're on), and a person checks the deck and decides:

| The deck shows | Press | Then |
|---|---|---|
| The step finished, fully or partly | **Succeeded** (resolve as done) | Rerun the pipeline with `-resume`. The result is read off the deck, well by well, so wells that got no liquid read 0 µL. |
| The material is spoiled, but the plate can be prepared again | **Failed** (void) | Prepare the plate again, load it, and rerun with `-resume`: the job runs again as a new attempt. |
| The material is gone for good | **Failed** (void) | Remove **all of that plate's rows** from the plate map, then rerun with `-resume`. |

Then press **Unload**. Unload is refused until Succeeded or Failed is pressed: an unresolved job would keep its key stuck, so every rerun would get the same failure back.

- **Don't remove only some wells of a plate.** A plate's job is identified by all its wells, so a trimmed plate is a new job: the robot would transfer the remaining wells again, including those that already got liquid. Press Succeeded instead, and let the wells read at 0 µL drop out downstream.
- **The resolution travels with the data.** The job's `attempt` and `reason` (e.g. `resolved as done by ana; was failed_in_protocol: tip pickup failed`) are in the task's `provenance.json`. In `examples/plate_transfer` they are also columns of `samples.csv` and of each plate report. `samples.csv` leaves out wells read at 0 µL, while the plate report keeps every well.

## Protocol parameters

A job's `params` set what its protocol runs with: `{"g": 500, "duration_s": 120}` for a `plate_spin`. A pipeline sets them per process with `ext.params` (see `wet-modules/README.md`); `labctl run --params '<json>'` sends them. Each protocol's names, types, defaults and the simulated machines' limits are in `PARAMS` (`src/nf_lab_adapter/bindings/__init__.py`); each wet module's `input.schema.json` lists the same names and types. At submit the adapter resolves the job's **program**: the binding's defaults, then the lab's, then the params asked for. It refuses the job (422, nothing queued) for an unknown param, a wrong type, a value out of range or one other than the lab fixed, so a typo never runs at the default. The job keeps both, `params` (as asked, part of the idempotency key) and `program` (as run), and so does the task's `provenance.json`. A job already in the ledger keeps the program it was queued with.

A lab narrows the machines' limits, sets its own defaults or fixes values in a TOML file named by `NF_LAB_LIMITS`, one table per protocol and param:

```toml
[plate_spin.g]
max = 500.0          # our rotor
[plate_incubate.temperature]
fixed = 37.0         # any other value is refused; asking for 37.0 is fine
[pcr.cycles]
default = 35
[reagent_dispense.reagent]
choices = ["master mix", "viability reagent", "DMSO"]
```

Keys: `min` and `max` (numbers), `choices` (strings), `default`, `fixed`. Every value must fit the machine's own limits in `PARAMS`; a file that doesn't (or names an unknown protocol or param, or holds `nan`) stops the adapter at start. The file applies to every robot that runs the protocol.

**Where to put it:** save it as `adapter/limits.toml`, next to the ledger, and start the adapter from `adapter/` with the variable set. The adapter only reads the file `NF_LAB_LIMITS` names: there is no default path, so without the variable the lab's limits are ignored. A relative path is read from the directory the adapter starts in.

```bash
cd adapter && NF_LAB_LIMITS=limits.toml NF_LAB_BACKEND=plr uv run uvicorn nf_lab_adapter.app:main --factory
```

The file is read once, at start: restart the adapter after changing it.

## Submit a job by hand

`labctl` is the same client a pipeline uses:

```bash
uv run labctl run --wait --capability liquid-handling --protocol plate_transfer --protocol-version 1.0 \
    --plate P1 --wells A1:S1 B1:S2 C1:NC1:negative_control
```

Submitting the same plate and wells again returns the existing job. Nothing runs twice. `labctl --help` lists its exit codes and the files it writes.

## From a pipeline

A wet process asks for a capability with `hints` and runs `labctl` in its script (see `examples/plate_transfer/`):

```groovy
process PLATE_TRANSFER {
    label 'process_wet'
    hints 'lab/capability': 'liquid-handling'
    ...
    script:
    """
    labctl run --wait \\
        --protocol plate_transfer \\
        ...
    """
}
```

The lab executor hands each wet task's `labctl` its capability (the `lab/capability` hint), its queue (the `queue` directive) and the run's session, as `NF_LAB_CAPABILITY`, `NF_LAB_QUEUE` and `NF_LAB_SESSION` in the task's environment, so a module names them once, as directives. They're per task and replace whatever the launch shell holds; a flag (`--capability`, `--queue`, `--session`) still wins, for `labctl` by hand. Each submit also tells the ledger which Nextflow task collects the job: its task hash (read from the work dir, and also its lineage id) and the session.

In `nextflow.config`, wet processes use the `lab` executor and retry only what is safe to retry:

```groovy
env.NF_LAB_ADAPTER = env('NF_LAB_ADAPTER') ?: 'http://127.0.0.1:8000'
process {
    withLabel: 'process_wet' {
        executor = 'lab'
        errorStrategy = { task.exitStatus in [69, 75] ? 'retry' : 'finish' }
    }
}
```

`labctl` exits with `0` when the job is done and with `75` when it failed before any liquid moved, which is safe to retry. `69` means the adapter couldn't be reached, so the outcome is unknown: the retry asks the ledger with the same key, so nothing runs twice. `1` covers every other failure, which needs a person. `finish`, not `terminate`, ends the run after a failure without stopping the other robots: a job already running completes its plate, and nothing new starts. Set `errorStrategy = 'finish'` for the dry processes too, so a software error doesn't stop the robots either. Aborting the run (Ctrl-C) stops a job safely if no liquid has moved yet, and otherwise lets it finish.

## The token

With `NF_LAB_TOKEN` set, the adapter refuses any request without it (401): the job API, the deck controls, `/devices` and `/queue`. Only the deck page's HTML and `/docs` load without it. Every client sends it:

- **A pipeline:** store it once as a Nextflow secret. The examples' config hands it to every task that calls the adapter when it exists, so the value never lands in the work dir (`.command.run` reads it from Nextflow's secrets store) or in `labctl.log`:

  ```bash
  nextflow secrets set NF_LAB_TOKEN <token>
  ```

  ```groovy
  process {
      withLabel: 'process_wet|process_lab' { secret = secrets.NF_LAB_TOKEN ? ['NF_LAB_TOKEN'] : [] }
  }
  ```

  `process_lab` marks a dry task that calls the adapter (`LAB_REPORT`); the wet tasks have `process_wet`.

  A wrong or missing token makes `labctl` exit 1: refused, not retried.
- **`labctl` by hand, the dashboard, the conformance kit:** `NF_LAB_TOKEN` in their environment. Never as an argument: a wet task's command line is saved in its work dir.
- **The deck page:** asks for the token when the adapter refuses it, and keeps it for that browser tab.

Plain HTTP sends the token in the clear: lab network only. Later: TLS through a reverse proxy in front of the adapter, and an operator token apart from the pipelines' token, so a pipeline can't void a job.

## The contract

`labctl` and the adapter talk through six calls: submit, status, lookup, safe_stop, release and session_jobs. `openapi.json` is that contract. Another adapter (for SiLA 2 or a vendor's API) can implement the same file, and the pipelines don't change. The deck page's calls (`/devices`, `/queue`, load, unload, e-stop, resolve) are not part of the contract: a real lab may do these in the vendor's software instead.

Regenerate `openapi.json` after changing the contract:

```bash
uv run python -m nf_lab_adapter.app > openapi.json
```

### Conformance kit

`conformance/` checks any adapter against `openapi.json`: it calls only the contract's operations, validates every reply against the file's schemas, and checks the guarantees (one key is one job, one job at a time per device, a safe stop never repeats material work, release only after the job finishes, a session lists its jobs). It needs only `pytest` and `jsonschema`, not this package. Point it at an adapter:

```bash
uv run pytest conformance --adapter http://127.0.0.1:8000    # its token, if any, in NF_LAB_TOKEN
```

Use a simulation adapter for testing, never one a lab is using: the kit submits real jobs for plates it makes up (`CK-…`), which on real robots would move liquid, and leaves them in the ledger. Each `--adapter` run starts with this warning. Loading a plate isn't part of the contract, so the adapter must load the queued plates itself (here: `NF_LAB_SIMULATED_OPERATOR=1`). Pick a capability and a protocol that succeeds with `--capability` and `--protocol` (defaults: `liquid-handling`, `plate_transfer`). Without `--adapter`, the kit starts this adapter once per backend on free ports, and runs once per binding and protocol in `OWN` (`conformance/conftest.py`): `FakeBackend` with `plate_transfer`, and the PyLabRobot adapter with `plate_transfer` (liquid handler), `plate_read` (plate reader) and `pcr` (thermocycler); plain `uv run pytest` does this too. A new operation in `openapi.json` fails the kit until it has a check.

## The lab report

A pipeline ends with `LAB_REPORT` (`examples/plate_transfer/modules/local/lab_report`), as an nf-core pipeline ends with MultiQC. It runs `labctl report --session ${workflow.sessionId}`, which writes `lab_report.json` and `lab_report.tsv`: every job that session submitted or collected, every attempt (failed ones too, which Nextflow's lineage doesn't record), each with the tasks that collected it as `lid://<task hash>`. The session ID is shared by a run and its `-resume`s, so the report of the run that completes includes the attempts of the runs that failed before it. Only this session's jobs: a ledger holds many runs.

What lineage and the ledger answer together, with the commands: [Questions lineage answers](../README.md#questions-lineage-answers).

## Tests

```bash
uv run pytest
```
