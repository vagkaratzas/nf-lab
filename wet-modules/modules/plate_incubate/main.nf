nextflow.enable.types = true

// Wet step: one plate_incubate run on a plate incubator, through the device adapter. One
// fixed program (result.json: program). Pre-flight heats the incubator, so taking the plate
// in starts the treatment: a safe stop before that leaves the plate on the loading tray,
// and after it the run finishes.
process PLATE_INCUBATE {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'incubation'

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
        --protocol plate_incubate \\
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
