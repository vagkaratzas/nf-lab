nextflow.enable.types = true

// End-to-end checks of the success criteria (checks/*/.checks, run by run.sh):
// one wet task per plate on the lab executor, through labctl and a FakeBackend
// adapter, then a dry task that can be made to fail.

params {
    plates: Integer = 1        // wet tasks: P1..Pn, one well each
    protocol: String = 'ok'    // FakeBackend script: ok, preflight_fail, preflight_once, protocol_fail, ...
    fail_dry: Boolean = false  // make the dry step fail
}

process WET {
    tag "${plate}"
    label 'process_wet'
    hints 'labrobotics/capability': 'liquid-handling'

    input:
    plate: String
    protocol: String

    output:
    file('result.json')

    script:
    """
    labctl run --wait --poll 0.2 \\
        --protocol ${protocol} \\
        --protocol-version 1.0 \\
        --plate ${plate} \\
        --wells A1:S1
    """
}

process DRY {
    input:
    result: Path
    fail: Boolean

    output:
    file('dry.txt')

    script:
    fail ? "exit 1" : "cat ${result} > dry.txt"
}

workflow {
    plates = channel.fromList((1..params.plates).collect { i -> "P${i}" })
    DRY(WET(plates, params.protocol), params.fail_dry)
}
