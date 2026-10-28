nextflow.enable.types = true

// Wet step: one dna_extraction run on a liquid handler, through the device adapter. The plate is
// an aliquot, controls and blanks included: lysis, then the lysate to a PCR plate. result.json
// gives each well's eluate_ul and yield_ng_ul (analyte DNA).
// Params: `ext.params`, a map of what input.schema.json lists, e.g. `withName: 'DNA_EXTRACTION' {
// ext.params = [lysis_ul: 40.0, eluate_ul: 150.0] }`. The lab's adapter adds its defaults,
// refuses unknown or out-of-range params (the task fails before the plate is touched) and records
// the program it ran in provenance.json.
process DNA_EXTRACTION {
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
        --protocol dna_extraction \\
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
