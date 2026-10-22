nextflow.enable.types = true

// Two machines, one plate: each plate of the plate map goes through a liquid handler
// (PLATE_TRANSFER), then a person moves it to a plate reader (PLATE_READ); both run in
// the WET subworkflow on the lab executor. ASSAY_REPORT (local) blank-corrects the readings.

include { WET } from './subworkflows/local/wet'
include { ASSAY_REPORT } from './modules/local/assay_report'

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
    wet = WET(plates)
    reports = ASSAY_REPORT(wet.read)
    samples = wet.read
        .join(wet.transferred.map { t -> record(id: t.id, transfer: t.provenance) }, by: 'id')
        .flatMap { p -> readWells(p) }
        .filter { w -> w.type == 'sample' }

    publish:
    reads = wet.read
    reports = reports
    samples = samples
}

// One record per well of a read plate, with the machine that filled it and the one that read it.
def readWells(p: Record) -> Iterable<Record> {
    def transfer = new groovy.json.JsonSlurper().parseText(p.transfer.text) as Map<String,?>
    def read = new groovy.json.JsonSlurper().parseText(p.provenance.text) as Map<String,?>
    return p.result.splitJson(path: 'wells').collect { w ->
        record(sample: w.sample, type: w.type, plate: p.id, well: "${w.row}${w.column}", absorbance: w.absorbance,
               transfer_device: transfer.device_id, read_device: read.device_id)
    }
}

output {
    reads: Channel<Record> {
        path { p -> "plates/${p.id}" }  // the read's result.json, provenance.json, labctl.log
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
