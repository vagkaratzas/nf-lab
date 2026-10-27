nextflow.enable.types = true

// Wet step: one qpcr run on a real-time PCR instrument, through the device adapter. The plate
// is an eluate (PCR) plate of RNA. One fixed one-step RT-qPCR program (result.json: program),
// read each cycle: a Ct per plate-map well, null when undetermined. Cycling can't be undone,
// so a safe stop before it leaves the plate as loaded, and after it the run finishes.
process QPCR {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'qpcr'

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
        --protocol qpcr \\
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
