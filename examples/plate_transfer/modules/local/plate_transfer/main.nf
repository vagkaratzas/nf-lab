nextflow.enable.types = true

// Wet step: one plate_transfer run on a liquid handler, through the device adapter.
// labctl (bin/) submits the plate's wells, or finds the job if the ledger already
// has this plate's key, waits, and writes result.json, provenance.json and labctl.log here.
// The protocol version is part of the script, so changing it re-runs the step.
process PLATE_TRANSFER {
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
        --capability liquid-handling \\
        --protocol plate_transfer \\
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
