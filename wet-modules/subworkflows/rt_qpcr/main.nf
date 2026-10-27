nextflow.enable.types = true

// The wet part of an RT-qPCR screen: each plate's RNA is extracted on a liquid handler into a
// PCR plate that keeps the plate's ID, then that plate goes through a real-time PCR instrument:
// a Ct per well. A person carries each plate between machines; each job waits in the adapter's
// queue until its plate is loaded, so many plates spread over the pools of both machines.

include { RNA_EXTRACTION } from '../../modules/rna_extraction'
include { QPCR } from '../../modules/qpcr'

workflow RT_QPCR {
    take:
    plates: Channel<Plate>

    main:
    extracted = RNA_EXTRACTION(plates)
    quantified = QPCR(plates.join(extracted, by: 'id'))  // the eluate plate exists once extraction is done

    emit:
    extracted = extracted
    quantified = quantified
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
