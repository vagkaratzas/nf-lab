nextflow.enable.types = true

// Wet step: one plate_split run on a liquid handler, through the device adapter.
// Each plate-map well goes into two aliquot plates (aliquot 1 and 2 in result.json);
// the pipeline names them. Moves liquid: past its first aspirate it runs to the end.
process PLATE_SPLIT {
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
    """
    labctl run --wait \\
        --session ${workflow.sessionId} \\
        --capability ${task.hints['lab/capability']} ${task.queue ? "--queue ${task.queue}" : ''} \\
        --protocol plate_split \\
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
