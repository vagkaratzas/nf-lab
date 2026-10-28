nextflow.enable.types = true

// Wet step: one heat_inactivation run on a heat block, through the device adapter. It runs the
// program the adapter resolves (result.json: program). Inactivation can't be undone, so a safe
// stop before the block heats leaves the plate as loaded, and after it the run finishes.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName:
// 'HEAT_INACTIVATION' { ext.params = [temperature: 80.0, duration_s: 600] }`. The lab's adapter
// adds its defaults, refuses unknown or out-of-range params (the task fails before the plate is
// touched) and records the program it ran in provenance.json.
process HEAT_INACTIVATION {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'temperature-control'

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
        --protocol heat_inactivation \\
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
