nextflow.enable.types = true

// nf-test's generated workflow script is untyped, so the channel it passes in isn't the typed
// channel this subworkflow's operators need: a pipeline test runs it from this typed script.
include { PARALLEL_EXTRACTION } from '../main.nf'

workflow {
    plates = channel.of(record(id: 'P6', wells: [
        record(row: 'A', column: 1, sample: 'S1', type: 'sample'),
        record(row: 'B', column: 1, sample: 'S2', type: 'sample'),
        record(row: 'C', column: 1, sample: 'NC1', type: 'negative_control'),
        record(row: 'D', column: 1, sample: 'BLK1', type: 'blank')
    ]))
    PARALLEL_EXTRACTION(plates)
}
