nextflow.enable.types = true

// The wet part of a cell viability assay (CellTiter-Glo style: how many cells survived each
// treatment?). The barcode is checked first; the reagent is pumped into every well, the plate is
// shaken to mix and incubated while the signal develops, then the reader measures each well.
// A person carries the plate between machines; each job waits in the adapter's queue until
// its plate is loaded, and each step starts once the one before it is done (the join on id).

include { PLATE_SCAN } from '../../modules/plate_scan'
include { REAGENT_DISPENSE } from '../../modules/reagent_dispense'
include { PLATE_MIX } from '../../modules/plate_mix'
include { PLATE_INCUBATE } from '../../modules/plate_incubate'
include { PLATE_READ } from '../../modules/plate_read'

workflow CELL_VIABILITY {
    take:
    plates: Channel<Plate>

    main:
    scanned = PLATE_SCAN(plates)
    dispensed = REAGENT_DISPENSE(plates.join(scanned, by: 'id'))
    mixed = PLATE_MIX(plates.join(dispensed, by: 'id'))
    incubated = PLATE_INCUBATE(plates.join(mixed, by: 'id'))
    read = PLATE_READ(plates.join(incubated, by: 'id'))

    emit:
    scanned = scanned
    dispensed = dispensed
    mixed = mixed
    incubated = incubated
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
