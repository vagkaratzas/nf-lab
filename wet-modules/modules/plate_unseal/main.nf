nextflow.enable.types = true

// Wet step: one plate_unseal run on a seal peeler, through the device adapter. One fixed
// program (result.json: program). The peel opens the wells, so a safe stop before it leaves
// the plate sealed, and after it the run finishes.
process PLATE_UNSEAL {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'peeling'

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
        --protocol plate_unseal \\
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
