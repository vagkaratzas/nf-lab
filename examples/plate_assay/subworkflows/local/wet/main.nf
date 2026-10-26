nextflow.enable.types = true

// The wet part of the assay: the same plate on two machines, a person carrying it between them.
// PLATE_READ's job for a plate waits in the adapter's queue until that plate is on a reader.

include { PLATE_TRANSFER } from '../../../../../wet-modules/modules/plate_transfer'
include { PLATE_READ } from '../../../../../wet-modules/modules/plate_read'

workflow WET {
    take:
    plates: Channel<Plate>

    main:
    transferred = PLATE_TRANSFER(plates)
    read = PLATE_READ(plates.join(transferred, by: 'id'))  // a plate is read once its transfer is done

    emit:
    transferred = transferred
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
