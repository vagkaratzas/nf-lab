nextflow.enable.types = true

// Wet step: one plate_transfer run on a liquid handler, through the device adapter. labctl (bin/)
// submits the plate's wells, or finds the job if the ledger already has this plate's key, waits,
// and writes result.json, provenance.json and labctl.log here. The protocol version is part of
// the script, so changing it re-runs the step.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'PLATE_TRANSFER' {
// ext.params = [volume_ul: 80.0] }`. The lab's adapter adds its defaults, refuses unknown or
// out-of-range params (the task fails before the plate is touched) and records the program it ran
// in provenance.json.
process PLATE_TRANSFER {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'liquid-handling'

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
        --session ${workflow.sessionId} \\
        --capability ${task.hints['lab/capability']} ${task.queue ? "--queue ${task.queue}" : ''} \\
        --protocol plate_transfer \\
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
