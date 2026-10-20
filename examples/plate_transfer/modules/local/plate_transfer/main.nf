nextflow.enable.types = true

// Wet step: one plate_transfer run on a liquid handler, through the device adapter.
// labctl (bin/) submits the job, or finds it if the ledger already has this plate's
// key, waits, and writes result.json, provenance.json and labctl.log here.
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
        samples: plate.samples,
        result: file('result.json'),
        provenance: file('provenance.json'),
        log: file('labctl.log')
    )

    script:
    """
    labctl run --wait \\
        --capability liquid-handling \\
        --protocol plate_transfer \\
        --protocol-version 1.0 \\
        --samples ${plate.samples.join(' ')}
    """
}

record Plate {
    id: String
    samples: List<String>  // in well order: the order is part of the idempotency key
}
