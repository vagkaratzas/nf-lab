nextflow.enable.types = true

// Wet step: one reagent_dispense run on a pump array, through the device adapter. It pumps the
// reagent and volume its program names (result.json: reagent). The first pump changes the plate,
// so a safe stop before it leaves the plate as loaded, and after it the run finishes.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'REAGENT_DISPENSE'
// { ext.params = [reagent: 'master mix', volume_ul: 20.0] }`. The lab's adapter adds its
// defaults, refuses unknown or out-of-range params (the task fails before the plate is touched)
// and records the program it ran in provenance.json.
process REAGENT_DISPENSE {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'pumping'

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
        --protocol reagent_dispense \\
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
