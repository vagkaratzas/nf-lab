# AGENTS.md

Nextflow executor plugin for robotic wet-lab steps (prototype). Design, concerns and plan live in the private `nextflow-robotics` notes repo.

- **Build:** `make assemble`, `make test`, `make install` (installs into `~/.nextflow/plugins`). Targets Nextflow 26.10.0 (`build.gradle`).
- **Java:** builds use SDKMAN Java 17, the default `$JAVA_HOME`. Don't point `JAVA_HOME` at base conda: its Java is 1.7 and its `jre/` breaks Gradle's TLS download. All other tools come from base conda (`~/miniconda3/bin/<tool>`).
