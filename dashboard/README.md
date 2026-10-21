# nf-lab dashboard

A live view of one lab run, for demos. It shows, refreshed every second:

1. **Nextflow**: each task's state, from the run's `.nextflow.log`.
2. **Adapter**: the calls the adapter received (submit, status, release, load…), from its request log. A job's status polls fold into one row with a count.
3. **Ledger**: the jobs, newest first, from the adapter's SQLite ledger. A row flashes when its state changes.
4. **Robots**: each robot's status, the job holding it, its heartbeat, and PyLabRobot's live view of its deck.

Next to them: the adapter's deck page, so you can load and unload plates from the dashboard.

The dashboard only reads: it opens the ledger read-only and never imports adapter code.

## Run

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
