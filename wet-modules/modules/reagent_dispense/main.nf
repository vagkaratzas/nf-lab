nextflow.enable.types = true

// Wet step: one reagent_dispense run on a pump array, through the device adapter. One fixed
// reagent and volume (result.json: reagent). The first pump changes the plate, so a safe
// stop before it leaves the plate as loaded, and after it the run finishes.
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
    """
    labctl run --wait \\
        --session ${workflow.sessionId} \\
        --capability ${task.hints['lab/capability']} ${task.queue ? "--queue ${task.queue}" : ''} \\
        --protocol reagent_dispense \\
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
