nextflow.enable.types = true

// RNA and DNA from one sample plate, side by side: each plate of the plate map is split on a
// liquid handler into two aliquot plates; RNA and DNA extraction run on them at the same time
// on two liquid handlers; each eluate then goes through PCR on a thermocycler (all in the
// PARALLEL_EXTRACTION subworkflow, on the lab executor). YIELD_QC (local) checks each
// extraction's yields. LAB_REPORT closes the run with the lab's side: every job of this session.

include { PARALLEL_EXTRACTION } from '../../wet-modules/subworkflows/parallel_extraction'
include { YIELD_QC } from './modules/local/yield_qc'
include { LAB_REPORT } from '../plate_transfer/modules/local/lab_report'
include { labPlate } from 'plugin/nf-lab'

params {
    plate_map: Channel<Well>  // CSV, one row per filled well: plate,row,column,sample,type
    min_yield: Float = 10.0   // ng/µL a sample needs to pass QC
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
    wet = PARALLEL_EXTRACTION(plates)
    qc = YIELD_QC(wet.extracted, params.min_yield)
    samples = wet.extracted
        .join(wet.amplified.map { a -> record(id: a.id, pcr: a.provenance) }, by: 'id')
        .flatMap { e -> extractedWells(e) }
        .filter { w -> w.type == 'sample' }
    provenance = wet.split.mix(wet.extracted).mix(wet.amplified).map { j -> j.provenance }
    lab_report = LAB_REPORT(provenance.collect())

    publish:
    split = wet.split
    extracted = wet.extracted
    amplified = wet.amplified
    qc = qc
    samples = samples
    lab_report = lab_report
}

// One record per well of an extracted plate, with the machine that extracted it and the one that ran its PCR.
def extractedWells(e: Record) -> Iterable<Record> {
    def pcr = labPlate(e.pcr)
    def extraction = labPlate(e.provenance, e.result)
    return extraction.wells.collect { w ->
        record(sample: w.sample, type: w.type, analyte: extraction.analyte, plate: extraction.plate, well: "${w.row}${w.column}",
               yield_ng_ul: w.yield_ng_ul, extraction_device: extraction.device_id, pcr_device: pcr.device_id)
    }
}

output {
    split: Channel<Record> {
        path { p -> "split/${p.id}" }  // result.json (the two aliquots), provenance.json, labctl.log
    }
    extracted: Channel<Record> {
        path { p -> "extraction/${p.id}" }  // per aliquot: eluate and yield per well
    }
    amplified: Channel<Record> {
        path { p -> "pcr/${p.id}" }  // per PCR plate: the program it went through
    }
    qc: Channel<Path> {
        path 'qc'
    }
    samples: Channel<Record> {
        index {
            path 'samples.csv'  // one row per sample well and analyte: what the dry steps join on `sample`
            header true
        }
    }
    lab_report: Set<Path> {
        path 'lab_report'  // lab_report.json and .tsv: every job of this session, failed attempts too
    }
}
