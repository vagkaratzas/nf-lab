nextflow.enable.types = true

// The wet part: each plate is split into two aliquot plates, <id>-RNA and <id>-DNA. Their
// extractions run side by side on two liquid handlers, each eluting into a PCR plate that
// keeps the aliquot's ID; each PCR plate then goes through the thermocycler. A person carries
// the plates between machines; each job waits in the adapter's queue until its plate is loaded.

include { PLATE_SPLIT } from '../../../modules/local/plate_split'
include { RNA_EXTRACTION } from '../../../modules/local/rna_extraction'
include { DNA_EXTRACTION } from '../../../modules/local/dna_extraction'
include { PCR } from '../../../modules/local/pcr'

workflow PARALLEL_EXTRACTION {
    take:
    plates: Channel<Plate>

    main:
    split = PLATE_SPLIT(plates)
    done = plates.join(split, by: 'id')  // the aliquots exist once the split is done
    rna_plates = done.map { p -> record(id: p.id + '-RNA', wells: p.wells) }
    dna_plates = done.map { p -> record(id: p.id + '-DNA', wells: p.wells) }
    rna = RNA_EXTRACTION(rna_plates)
    dna = DNA_EXTRACTION(dna_plates)
    amplified = PCR(rna_plates.join(rna, by: 'id').mix(dna_plates.join(dna, by: 'id')))

    emit:
    split = split
    extracted = rna.mix(dna)
    amplified = amplified
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
