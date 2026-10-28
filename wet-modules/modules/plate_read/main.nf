nextflow.enable.types = true

// Wet step: one plate_read run on a plate reader, through the device adapter. Reading moves no
// liquid: a failed or safe-stopped read is retryable (exit 75); an e-stopped one waits for a
// person's Failed, then reruns with -resume.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'PLATE_READ' {
// ext.params = [wavelength_nm: 600] }`. The lab's adapter adds its defaults, refuses unknown or
// out-of-range params (the task fails before the plate is touched) and records the program it ran
// in provenance.json.
process PLATE_READ {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'plate-reading'

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
        --protocol plate_read \\
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
