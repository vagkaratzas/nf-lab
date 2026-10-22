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
| `NF_LAB_BACKEND` | `fake` | `plr`: PyLabRobot's simulated liquid handler. `fake`: a scripted device for tests |
| `NF_LAB_DEVICES` | `1` | How many robots, all with the `liquid-handling` capability (`plr-sim-1`, `plr-sim-2`… or `fake-1`…); or pools as `capability[/queue]:count,…`, e.g. `liquid-handling/opentrons:2,liquid-handling/hamilton:1` (`opentrons-1`, `opentrons-2`, `hamilton-1`). A job naming a queue (`labctl --queue`, from Nextflow's `queue` directive) runs only on that queue's robots |
| `NF_LAB_VISUALIZER` | off | `1`: each PyLabRobot robot serves a live view of its deck (port 1337, then 1338…), linked from its row on the deck page |
| `NF_LAB_SIMULATED_OPERATOR` | on for `fake`, off for `plr` | `1`: nobody at the bench is needed, because the adapter loads and unloads plates itself. Failed jobs still wait for a person |
| `NF_LAB_LEDGER` | `ledger.sqlite` | The ledger file. Keep it between runs: it is what stops a job from running twice |
| `NF_LAB_STEP` | `1.0` | Seconds per simulated robot step |
| `NF_LAB_SILENT_AFTER` | `600` | Seconds without a report from a robot before its job fails as `device_silent` |

## Three questions the adapter answers

### What can each robot do?

The deck page lists each robot's protocols under its name. For the same list without the page:

```bash
curl -s localhost:8000/devices | jq '.[] | {device_id, capability, protocols}'
```

A job asks for a **capability** (`liquid-handling`), not a robot. The PyLabRobot backend runs one protocol, `plate_transfer`: it fills each listed well of the destination plate from the same well of the sample plate (controls and blanks: from the buffer plate). The fake backend runs `plate_transfer` too, instantly and with the same result shape, so `examples/plate_transfer` runs without PyLabRobot; its other protocols (`ok`, `preflight_fail`, `protocol_fail`…) are scripted failures for tests.

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
    labctl run --wait --session ${workflow.sessionId} --capability ${task.hints['lab/capability']} --protocol plate_transfer ...
    """
}
```

Each submit also tells the ledger which Nextflow task collects the job: its task hash (read from the work dir, and also its lineage id) and the session from `--session`.

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

## The contract

`labctl` and the adapter talk through six calls: submit, status, lookup, safe_stop, release and session_jobs. `openapi.json` is that contract. Another adapter (for SiLA 2 or a vendor's API) can implement the same file, and the pipelines don't change. The deck page's calls (`/devices`, `/queue`, load, unload, e-stop, resolve) are not part of the contract: a real lab may do these in the vendor's software instead.

Regenerate `openapi.json` after changing the contract:

```bash
uv run python -m nf_lab_adapter.app > openapi.json
```

## Tests

```bash
uv run pytest
```
