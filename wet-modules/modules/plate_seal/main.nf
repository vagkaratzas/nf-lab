nextflow.enable.types = true

// Wet step: one plate_seal run on a plate sealer, through the device adapter. It runs the program
// the adapter resolves (result.json: program). A seal changes the plate (removing it is a
// plate_unseal job), so a safe stop before the seal leaves the plate as loaded, and after it the
// run finishes.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'PLATE_SEAL' {
// ext.params = [temperature: 170, duration_s: 2.0] }`. The lab's adapter adds its defaults,
// refuses unknown or out-of-range params (the task fails before the plate is touched) and records
// the program it ran in provenance.json.
process PLATE_SEAL {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'sealing'

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
        --protocol plate_seal \\
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
