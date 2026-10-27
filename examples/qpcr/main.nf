nextflow.enable.types = true

// An RT-qPCR screen at scale: 1000 samples on 11 plates (92 samples and 4 controls each). Each
// plate's RNA is extracted on one of the lab's liquid handlers, then its eluate plate runs
// one-step RT-qPCR on one of its qPCR instruments (the RT_QPCR subworkflow, on the lab executor).
// Plates queue in the adapter and spread over both pools as robots free up. The calls are dry:
// a plate counts only if its controls pass. LAB_REPORT closes the run with the lab's side.

include { RT_QPCR } from '../../wet-modules/subworkflows/rt_qpcr'
include { LAB_REPORT } from '../plate_transfer/modules/local/lab_report'
include { labPlate } from 'plugin/nf-lab'

params {
    plate_map: Channel<Well>  // CSV, one row per filled well: plate,row,column,sample,type
    max_ct: Float = 35.0      // a sample with a Ct below this is detected
}

record Well {
    plate: String
    row: String       // A-P
    column: Integer   // 1-24
    sample: String    // the sample ID, or a control's or blank's own ID
    type: String      // sample, negative_control (no-template control), positive_control or blank (extraction blank)
}

workflow {
    main:
    plates = params.plate_map
        .map { w -> tuple(w.plate, w) }
        .groupBy()
        // groupBy gives an unordered Bag; sorting keeps the task hash stable (labctl's key ignores order)
        .map { plate, wells -> record(id: plate, wells: wells.toSorted { a, b -> a.row <=> b.row ?: a.column <=> b.column }) }
    wet = RT_QPCR(plates)
    calls = wet.quantified
        .join(wet.extracted.map { e -> record(id: e.id, extraction: e.provenance) }, by: 'id')
        .flatMap { q -> calledWells(q, params.max_ct) }
    samples = calls.filter { w -> w.type == 'sample' }
    provenance = wet.extracted.mix(wet.quantified).map { j -> j.provenance }
    lab_report = LAB_REPORT(provenance.collect())

    publish:
    extracted = wet.extracted
    quantified = wet.quantified
    samples = samples
    lab_report = lab_report
}

// One record per well of a qPCR plate: its Ct, its call, and the machines that extracted and amplified it.
// The plate's controls decide whether its samples are called at all: no Ct in a no-template control or
// the extraction blank (no contamination), a Ct in the positive control (the run worked).
def calledWells(q: Record, maxCt: Float) -> Iterable<Record> {
    def qpcr = labPlate(q.provenance, q.result)
    def extraction = labPlate(q.extraction)
    def ok = qpcr.wells.every { w ->
        w.type == 'sample' || (w.type == 'positive_control' ? w.ct != null : w.ct == null)
    }
    return qpcr.wells.collect { w ->
        def call = !ok ? 'invalid_plate' : (w.ct != null && w.ct < maxCt ? 'detected' : 'not_detected')
        record(sample: w.sample, type: w.type, plate: qpcr.plate, well: "${w.row}${w.column}", ct: w.ct ?: '',
               call: call, extraction_device: extraction.device_id, qpcr_device: qpcr.device_id)
    }
}

output {
    extracted: Channel<Record> {
        path { p -> "extraction/${p.id}" }  // per plate: eluate and yield per well
    }
    quantified: Channel<Record> {
        path { p -> "qpcr/${p.id}" }  // per plate: the program and a Ct per well
    }
    samples: Channel<Record> {
        index {
            path 'samples.csv'  // one row per sample: Ct, call, the robots that handled it
            header true
        }
    }
    lab_report: Set<Path> {
        path 'lab_report'  // lab_report.json and .tsv: every job of this session, failed attempts too
    }
}
