nextflow.enable.types = true

// The wet part of preparing compound plates (chemistry or screening labs): solid is dosed into
// each well, the plate is weighed to check the dose before anything is added to it, solvent is
// pumped in and the plate is shaken until the solid dissolves.
// A person carries the plate between machines; each job waits in the adapter's queue until
// its plate is loaded, and each step starts once the one before it is done (the join on id).

include { POWDER_DISPENSE } from '../../modules/powder_dispense'
include { PLATE_WEIGH } from '../../modules/plate_weigh'
include { REAGENT_DISPENSE } from '../../modules/reagent_dispense'
include { PLATE_MIX } from '../../modules/plate_mix'

workflow COMPOUND_PREP {
    take:
    plates: Channel<Plate>

    main:
    dosed = POWDER_DISPENSE(plates)
    weighed = PLATE_WEIGH(plates.join(dosed, by: 'id'))
    dissolved = REAGENT_DISPENSE(plates.join(weighed, by: 'id'))
    mixed = PLATE_MIX(plates.join(dissolved, by: 'id'))

    emit:
    dosed = dosed
    weighed = weighed
    dissolved = dissolved
    mixed = mixed
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
