# AGENTS.md

Nextflow executor plugin for robotic wet-lab steps (prototype). Design, concerns and plan live in the control-panel repo: `/home/vangelis/Documents/10_Projects/Seqera/Nextflow Robotics` (https://github.com/vagkaratzas/nextflow-robotics, private). Read its `AGENTS.md` and `docs/DESIGN.md` first; record findings and decisions there, not here.

- **Build:** `make assemble`, `make test`, `make install` (installs into `~/.nextflow/plugins`). Targets Nextflow 26.10.0 (`build.gradle`).
- **Adapter (`adapter/`):** Python 3.12 via `uv` (`~/.local/bin/uv`), not base conda. `cd adapter && uv sync`, then `uv run pytest` / `uv run uvicorn ...`. Add deps with `uv add` (dev: `uv add --dev`).
- **Java:** builds use SDKMAN Java 17, the default `$JAVA_HOME`. Don't point `JAVA_HOME` at base conda: its Java is 1.7 and its `jre/` breaks Gradle's TLS download. All other tools come from base conda (`~/miniconda3/bin/<tool>`).
