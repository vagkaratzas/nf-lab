nextflow.enable.types = true

// The wet part of a PCR set-up, as most labs run it: the plate's barcode is checked first, so a
// wrong plate fails before anything is added. Master mix is pumped into every well and the samples
// are pipetted in; the plate is sealed so nothing evaporates while cycling and spun so every drop
// sits at the bottom of its well. After cycling the seal is peeled off for whatever comes next.
// A person carries the plate between machines; each job waits in the adapter's queue until
// its plate is loaded, and each step starts once the one before it is done (the join on id).

include { PLATE_SCAN } from '../../modules/plate_scan'
include { REAGENT_DISPENSE } from '../../modules/reagent_dispense'
include { PLATE_TRANSFER } from '../../modules/plate_transfer'
include { PLATE_SEAL } from '../../modules/plate_seal'
include { PLATE_SPIN } from '../../modules/plate_spin'
include { PCR } from '../../modules/pcr'
include { PLATE_UNSEAL } from '../../modules/plate_unseal'

workflow PCR_SETUP {
    take:
    plates: Channel<Plate>

    main:
    scanned = PLATE_SCAN(plates)
    dispensed = REAGENT_DISPENSE(plates.join(scanned, by: 'id'))
    transferred = PLATE_TRANSFER(plates.join(dispensed, by: 'id'))
    sealed = PLATE_SEAL(plates.join(transferred, by: 'id'))
    spun = PLATE_SPIN(plates.join(sealed, by: 'id'))
    amplified = PCR(plates.join(spun, by: 'id'))
    unsealed = PLATE_UNSEAL(plates.join(amplified, by: 'id'))

    emit:
    scanned = scanned
    dispensed = dispensed
    transferred = transferred
    sealed = sealed
    spun = spun
    amplified = amplified
    unsealed = unsealed
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
