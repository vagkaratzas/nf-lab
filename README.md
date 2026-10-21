# nf-lab plugin

The device adapter (and its deck page and `labctl`) has its own guide: [adapter/README.md](adapter/README.md).

## Building

To build the plugin:
```bash
make assemble
```

## Testing with Nextflow

The plugin can be tested without a local Nextflow installation:

1. Build and install the plugin to your local Nextflow installation: `make install`
2. Run a pipeline with the plugin: `nextflow run hello -plugins nf-lab@0.1.0`

## Tests

Prerequisites: Java 17, [uv](https://docs.astral.sh/uv/), `nextflow` on `PATH`.

```bash
make test                                         # plugin unit tests (Gradle)
(cd adapter && uv sync && uv run pytest)          # device adapter and labctl
make install && validation/run.sh                 # end-to-end checks of the success criteria
validation/run.sh abort-after-boundary            # one check; its log: build/validation/<name>/checks.out
```

The end-to-end checks run the `validation/` pipeline against a simulated device adapter (one per check, on a free port) and check the guarantees across runs: `-resume` after a dry failure, a fresh launch dir, two wet tasks serial on one robot, abort before and after the stop boundary, retries. One check per `validation/checks/<name>/.checks`.

## Publishing

Plugins can be published to a central plugin registry to make them accessible to the Nextflow community. 


Follow these steps to publish the plugin to the Nextflow Plugin Registry:

1. Create a file named `$HOME/.gradle/gradle.properties`, where $HOME is your home directory. Add the following properties:

    * `npr.apiKey`: Your Nextflow Plugin Registry access token.

2. Use the following command to package and create a release for your plugin on GitHub: `make release`.


> [!NOTE]
> The Nextflow Plugin registry is currently available as preview technology. Contact info@nextflow.io to learn how to get access to it.
> 
