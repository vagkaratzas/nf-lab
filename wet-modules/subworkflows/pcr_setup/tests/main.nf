nextflow.enable.types = true

// nf-test's generated workflow script is untyped, so the channel it passes in isn't the typed
// channel this subworkflow's operators need: a pipeline test runs it from this typed script.
include { PCR_SETUP } from '../main.nf'

workflow {
    plates = channel.of(record(id: 'P22', wells: [
        record(row: 'A', column: 1, sample: 'S1', type: 'sample'),
        record(row: 'B', column: 1, sample: 'S2', type: 'sample'),
        record(row: 'A', column: 12, sample: 'NEG1', type: 'negative_control'),
        record(row: 'B', column: 12, sample: 'POS1', type: 'positive_control'),
        record(row: 'C', column: 12, sample: 'BLK1', type: 'blank')
    ]))
    PCR_SETUP(plates)
}
