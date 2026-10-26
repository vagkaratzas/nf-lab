nextflow.enable.types = true

// Wet step: one pcr run on a thermocycler, through the device adapter. The plate is an
// eluate (PCR) plate. One fixed program (result.json: program); cycling can't be undone,
// so a safe stop before it leaves the plate as loaded, and after it the run finishes.
process PCR {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'thermocycling'

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
    """
    labctl run --wait \\
        --session ${workflow.sessionId} \\
        --capability ${task.hints['lab/capability']} ${task.queue ? "--queue ${task.queue}" : ''} \\
        --protocol pcr \\
        --protocol-version 1.0 \\
        --plate ${plate.id} \\
        --wells ${wells}
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
