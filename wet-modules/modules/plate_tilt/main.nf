nextflow.enable.types = true

// Wet step: one plate_tilt run on a plate tilter, through the device adapter. One fixed
// program (result.json: program). A tilt moves liquid, so it counts as a change: a safe stop
// before the tilt leaves the plate as loaded, and after it the run finishes.
process PLATE_TILT {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'tilting'

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
        --protocol plate_tilt \\
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
