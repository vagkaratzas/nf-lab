nextflow.enable.types = true

// Wet step: one qpcr run on a real-time PCR instrument, through the device adapter. The plate is
// an eluate (PCR) plate of RNA. It runs a one-step RT-qPCR program (result.json: program), read
// each cycle: a Ct per plate-map well, null when undetermined. Cycling can't be undone, so a safe
// stop before it leaves the plate as loaded, and after it the run finishes.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'QPCR' {
// ext.params = [cycles: 45, channel: 'HEX'] }`. The lab's adapter adds its defaults, refuses
// unknown or out-of-range params (the task fails before the plate is touched) and records the
// program it ran in provenance.json.
process QPCR {
    tag "${plate.id}"
    label 'process_wet'
    hints 'labrobotics/capability': 'qpcr'

    input:
    plate: Plate

    output:
    record(
        id: plate.id,
        result: file('result.json'),
        provenance: file('provenance.json'),
        log: file('labctl.log')
    )

    script:
    def wells = plate.wells.collect { w -> "${w.row}${w.column}:${w.sample}:${w.type}" }.join(' ')
    def params_json = groovy.json.JsonOutput.toJson(task.ext.params)
    """
    labctl run --wait \\
        --protocol qpcr \\
        --protocol-version 1.0 \\
        --plate ${plate.id} \\
        --wells ${wells} \\
        --params - <<'JSON'
    ${params_json}
    JSON
    """
}

record Plate {
    id: String
    wells: List<PlateWell>
}

record PlateWell {
    row: String
    column: Integer
    sample: String
    type: String
}
