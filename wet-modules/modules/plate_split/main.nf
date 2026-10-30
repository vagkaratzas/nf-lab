nextflow.enable.types = true

// Wet step: one plate_split run on a liquid handler, through the device adapter. Each plate-map
// well goes into two aliquot plates (aliquot 1 and 2 in result.json); the pipeline names them.
// Moves liquid: past its first aspirate it runs to the end.
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'PLATE_SPLIT' {
// ext.params = [volume_ul: 120.0] }`. The lab's adapter adds its defaults, refuses unknown or
// out-of-range params (the task fails before the plate is touched) and records the program it ran
// in provenance.json.
process PLATE_SPLIT {
    tag "${plate.id}"
    label 'process_wet'
    hints 'labrobotics/capability': 'liquid-handling'

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
        --protocol plate_split \\
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
