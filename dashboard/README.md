# nf-lab dashboard

A live view of one lab run, for demos. It shows, refreshed every second:

1. **Nextflow**: each task's state, from the run's `.nextflow.log`.
2. **Adapter**: the calls the adapter received (submit, status, release, load…), from its request log. A job's status polls fold into one row with a count.
3. **Ledger**: the jobs, newest first, from the adapter's SQLite ledger. A row flashes when its state changes.
4. **Robots**: each robot's status, the job holding it, its heartbeat, and PyLabRobot's live view of its deck.

Next to them: the adapter's deck page, so you can load and unload plates from the dashboard.

The panels only read: they open the ledger read-only. The demo bar is the one part that acts, and only as a person would: it starts processes and presses deck buttons through the adapter's API. Nothing imports adapter code.

## Demos

The bar at the top runs scripted demos of the example you pick (`EXAMPLES` in `demo.py`): `plate_transfer` (two liquid handlers) or `plate_assay` (two liquid handlers and a plate reader; Manual only, its Pool and E-stop aren't scripted). Each demo starts fresh: it starts its own adapter (a new ledger, the example's simulated PyLabRobot machines, empty decks), runs the example, and plays the person at the deck, loading and unloading plates and pressing E-stop or Failed through the adapter's operator API. A bubble narrates each step, and the robot a person step acts on flashes in panel 4.

- **Pool**: four plates over two robots; the operator loads plates as robots free up. About 2.5 minutes.
- **E-stop**: the operator e-stops one robot mid-plate. The other robot finishes (`errorStrategy 'finish'`), the run ends failed; the operator presses Failed and unloads; a `-resume` rerun runs only that plate again, as attempt 2. About 4 minutes.

- **Manual**: the same fresh start and the pipeline running, then nothing scripted: you are the operator on the deck controls.

**↻ Rerun (-resume)** runs the pipeline again in the last demo's folder, on the same ledger (e.g. after you pressed Failed on an e-stopped plate); it starts the adapter again on that ledger if it isn't up.

**Stop** ends the pipeline (TERM: jobs before the stop boundary are safe-stopped) and leaves the adapter up. There is no pause: Nextflow and the robots can't be paused, so a paused narrator would fall out of step with them. The demos run slowly instead (`NF_LAB_DEMO_STEP`).

For the demos, start only the dashboard: they need port 8000 free, so stop any adapter you started yourself. Each demo keeps its ledger, logs and results in `build/demo/<example>/<demo>/`, and the panels read from there once a demo has run. `nextflow` must be on `PATH` (or set `NXF_CMD`), with the plugin installed (`make install`).

```bash
cd dashboard && uv run uvicorn nf_lab_dashboard.app:main --factory --port 8080
```

Settings: `NF_LAB_DEMO_BEAT` (seconds before each person step, and the least a narration line stays up, default 5), `NF_LAB_DEMO_STEP` (seconds per robot step, default 1.5), `NF_LAB_DEMO_BACKEND` (`plr`, or `fake` without deck views).

## Run with your own adapter

Three shells, from the nf-lab root:

```bash
# 1. the adapter: two simulated robots
cd adapter && NF_LAB_BACKEND=plr NF_LAB_VISUALIZER=1 NF_LAB_DEVICES=2 uv run uvicorn nf_lab_adapter.app:main --factory
# 2. the dashboard
cd dashboard && uv run uvicorn nf_lab_dashboard.app:main --factory --port 8080
# 3. the pipeline
cd examples/plate_transfer && nextflow run . -ansi-log false
```

Open http://127.0.0.1:8080.

The defaults match these commands. Change them with:

| Variable | Default |
|---|---|
| `NF_LAB_ADAPTER` | `http://127.0.0.1:8000` |
| `NF_LAB_LEDGER` | `adapter/ledger.sqlite` (the request log is `<ledger>.requests.jsonl` next to it) |
| `NF_LAB_NEXTFLOW_LOG` | `examples/plate_transfer/.nextflow.log` |

## Tests

```bash
uv run pytest
```
