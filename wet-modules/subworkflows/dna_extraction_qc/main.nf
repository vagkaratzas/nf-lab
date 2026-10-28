nextflow.enable.types = true

// The wet part of DNA extraction with a yield check: each plate's cells are broken open on a
// heater-shaker, the plate is spun to pellet the debris, the DNA is extracted on a liquid handler
// into a plate that keeps the plate's ID, and the reader measures each eluate well (absorbance) so
// a failed extraction shows before the DNA goes on to anything expensive.
// A person carries the plate between machines; each job waits in the adapter's queue until
// its plate is loaded, and each step starts once the one before it is done (the join on id).

include { PLATE_LYSIS } from '../../modules/plate_lysis'
include { PLATE_SPIN } from '../../modules/plate_spin'
include { DNA_EXTRACTION } from '../../modules/dna_extraction'
include { PLATE_READ } from '../../modules/plate_read'

workflow DNA_EXTRACTION_QC {
    take:
    plates: Channel<Plate>

    main:
    lysed = PLATE_LYSIS(plates)
    spun = PLATE_SPIN(plates.join(lysed, by: 'id'))
    extracted = DNA_EXTRACTION(plates.join(spun, by: 'id'))
    read = PLATE_READ(plates.join(extracted, by: 'id'))

    emit:
    lysed = lysed
    spun = spun
    extracted = extracted
    read = read
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
