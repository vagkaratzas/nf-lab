nextflow.enable.types = true

// Wet + dry: each plate of the plate map goes through a simulated liquid handler
// (PLATE_TRANSFER, lab executor), then a report (PLATE_REPORT, local). The wet
// result is flattened back to one record per well; the sample wells go on.
// LAB_REPORT closes the run with the lab's side: every job of this session, failures too.

include { PLATE_TRANSFER } from './modules/local/plate_transfer'
include { PLATE_REPORT } from './modules/local/plate_report'
include { LAB_REPORT } from './modules/local/lab_report'
include { labPlate } from 'plugin/nf-lab'

params {
    plate_map: Channel<Well>  // CSV, one row per filled well: plate,row,column,sample,type
}

record Well {
    plate: String
    row: String       // A-P
    column: Integer   // 1-24
    sample: String    // the sample ID, or a control's or blank's own ID
    type: String      // sample, negative_control or blank
}

workflow {
    main:
    plates = params.plate_map
        .map { w -> tuple(w.plate, w) }
        .groupBy()
        // groupBy gives an unordered Bag; sorting keeps the task hash stable (labctl's key ignores order)
        .map { plate, wells -> record(id: plate, wells: wells.toSorted { a, b -> a.row <=> b.row ?: a.column <=> b.column }) }
    transferred = PLATE_TRANSFER(plates)
    reports = PLATE_REPORT(transferred)
    samples = transferred
        .flatMap { p -> transferredWells(p) }
        // controls, blanks and wells read at 0 µL (e.g. a job a person resolved as done) stay in the plate report
        .filter { w -> w.type == 'sample' && (w.volume_ul as Double) > 0 }
    lab_report = LAB_REPORT(transferred.map { p -> p.provenance }.collect())

    publish:
    plates = transferred
    reports = reports
    samples = samples
    lab_report = lab_report
}

// One record per well of a transferred plate: labPlate gives the plate's job and wells as data.
// attempt and reason say how the job ended, e.g. "resolved as done by ana; was failed_in_protocol: ...".
def transferredWells(p: Record) -> Iterable<Record> {
    def plate = labPlate(p.provenance, p.result)
    return plate.wells.collect { w ->
        record(sample: w.sample, type: w.type, plate: plate.plate, well: "${w.row}${w.column}",
               volume_ul: w.volume_ul, job_id: plate.job_id, device_id: plate.device_id,
               attempt: plate.attempt, reason: plate.reason ?: '')
    }
}

output {
    plates: Channel<Record> {
        path { p -> "plates/${p.id}" }  // result.json, provenance.json, labctl.log
    }
    reports: Channel<Path> {
        path 'reports'
    }
    samples: Channel<Record> {
        index {
            path 'samples.csv'  // one row per sample well: what the dry steps join on `sample`
            header true
        }
    }
    lab_report: Set<Path> {
        path 'lab_report'  // lab_report.json and .tsv: every job of this session, failed attempts too
    }
}
