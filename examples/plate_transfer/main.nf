nextflow.enable.types = true

// Wet + dry: each plate of the plate map goes through a simulated liquid handler
// (PLATE_TRANSFER, lab executor), then a report (PLATE_REPORT, local). The wet
// result is flattened back to one record per well; the sample wells go on.

include { PLATE_TRANSFER } from './modules/local/plate_transfer'
include { PLATE_REPORT } from './modules/local/plate_report'

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
        .filter { w -> w.type == 'sample' }  // controls and blanks stay in the plate report

    publish:
    plates = transferred
    reports = reports
    samples = samples
}

// One record per well of a transferred plate, from its result.json and provenance.json.
def transferredWells(p: Record) -> Iterable<Record> {
    def job = new groovy.json.JsonSlurper().parseText(p.provenance.text) as Map<String,?>
    return p.result.splitJson(path: 'wells').collect { w ->
        record(sample: w.sample, type: w.type, plate: p.id, well: "${w.row}${w.column}",
               volume_ul: w.volume_ul, job_id: job.job_id, device_id: job.device_id)
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
}
