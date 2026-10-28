nextflow.enable.types = true

// Wet step: one plate_scan run on a barcode scanner, through the device adapter. A barcode
// that isn't the job's plate fails the run. Scanning changes nothing: a failed or
// safe-stopped scan is retryable (exit 75); an e-stopped one waits for a person's Failed,
// then reruns with -resume.
process PLATE_SCAN {
    tag "${plate.id}"
    label 'process_wet'
    hints 'lab/capability': 'barcode-reading'

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
        --protocol plate_scan \\
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
