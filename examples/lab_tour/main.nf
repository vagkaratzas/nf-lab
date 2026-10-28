nextflow.enable.types = true

// A tour of the lab: five plates, one per routine, run side by side, so every simulated machine
// gets work. The plate map's `routine` column picks each plate's routine:
//   pcr_setup            barcode check, master mix, samples, seal, spin, PCR, peel
//   cell_viability       barcode check, viability reagent, mix, incubate, read
//   dna_extraction_qc    lyse, spin, DNA extraction, read the yield
//   compound_prep        dose powder, weigh, add solvent, mix
//   inactivated_rt_qpcr  heat-inactivate, tilt, RNA extraction, RT-qPCR
// Routines share machines (the centrifuge, the shaker, the plate reader, the liquid handlers), so
// plates queue for them in the adapter. STEPS lists every job: which robot ran which step of which
// plate, and when. LAB_REPORT closes the run with the lab's side.

include { PCR_SETUP } from '../../wet-modules/subworkflows/pcr_setup'
include { CELL_VIABILITY } from '../../wet-modules/subworkflows/cell_viability'
include { DNA_EXTRACTION_QC } from '../../wet-modules/subworkflows/dna_extraction_qc'
include { COMPOUND_PREP } from '../../wet-modules/subworkflows/compound_prep'
include { RT_QPCR } from '../../wet-modules/subworkflows/rt_qpcr'
include { HEAT_INACTIVATION } from '../../wet-modules/modules/heat_inactivation'
include { PLATE_TILT } from '../../wet-modules/modules/plate_tilt'
include { LAB_REPORT } from '../plate_transfer/modules/local/lab_report'
include { labPlate } from 'plugin/nf-lab'

params {
    plate_map: Channel<Well>  // CSV, one row per filled well: plate,routine,row,column,sample,type
}

record Well {
    plate: String
    routine: String   // pcr_setup, cell_viability, dna_extraction_qc, compound_prep or inactivated_rt_qpcr
    row: String       // A-P
    column: Integer   // 1-24
    sample: String    // the sample ID, or a control's or blank's own ID
    type: String      // sample, negative_control, positive_control or blank
}

workflow {
    main:
    plates = params.plate_map
        .map { w -> tuple(w.plate, w) }
        .groupBy()
        // groupBy gives an unordered Bag; sorting keeps the task hash stable (labctl's key ignores order)
        .map { plate, wells ->
            def sorted = wells.toSorted { a, b -> a.row <=> b.row ?: a.column <=> b.column }
            record(id: plate, routine: sorted[0].routine, wells: sorted)
        }

    pcr = PCR_SETUP(plates.filter { p -> p.routine == 'pcr_setup' })
    cells = CELL_VIABILITY(plates.filter { p -> p.routine == 'cell_viability' })
    dna = DNA_EXTRACTION_QC(plates.filter { p -> p.routine == 'dna_extraction_qc' })
    compounds = COMPOUND_PREP(plates.filter { p -> p.routine == 'compound_prep' })
    rna_plates = plates.filter { p -> p.routine == 'inactivated_rt_qpcr' }
    inactivated = HEAT_INACTIVATION(rna_plates)  // kill any virus before anyone opens the plate
    tilted = PLATE_TILT(rna_plates.join(inactivated, by: 'id'))  // so the liquid handler reaches the last drop
    rna = RT_QPCR(rna_plates.join(tilted, by: 'id').map { p -> record(id: p.id, wells: p.wells) })

    jobs = pcr.scanned
        .mix(pcr.dispensed)
        .mix(pcr.transferred)
        .mix(pcr.sealed)
        .mix(pcr.spun)
        .mix(pcr.amplified)
        .mix(pcr.unsealed)
        .mix(cells.scanned)
        .mix(cells.dispensed)
        .mix(cells.mixed)
        .mix(cells.incubated)
        .mix(cells.read)
        .mix(dna.lysed)
        .mix(dna.spun)
        .mix(dna.extracted)
        .mix(dna.read)
        .mix(compounds.dosed)
        .mix(compounds.weighed)
        .mix(compounds.dissolved)
        .mix(compounds.mixed)
        .mix(inactivated)
        .mix(tilted)
        .mix(rna.extracted)
        .mix(rna.quantified)
    steps = jobs.map { j -> step(j.provenance) }
    lab_report = LAB_REPORT(jobs.map { j -> j.provenance }.collect())

    publish:
    pcr = pcr.amplified
    viability = cells.read
    dna_yield = dna.read
    compounds = compounds.weighed
    qpcr = rna.quantified
    steps = steps
    lab_report = lab_report
}

// One row per job: the plate, the step, the robot that ran it and when.
def step(provenance: Path) -> Record {
    def job = labPlate(provenance)
    return record(plate: job.plate, protocol: job.protocol, capability: job.capability, device: job.device_id,
                  started_at: job.started_at, finished_at: job.finished_at)
}

output {
    pcr: Channel<Record> {
        path { p -> "pcr/${p.id}" }  // the thermocycling program the plate went through
    }
    viability: Channel<Record> {
        path { p -> "viability/${p.id}" }  // the signal per well
    }
    dna_yield: Channel<Record> {
        path { p -> "dna_yield/${p.id}" }  // the reading per eluate well
    }
    compounds: Channel<Record> {
        path { p -> "compounds/${p.id}" }  // the dosed plate's weight
    }
    qpcr: Channel<Record> {
        path { p -> "qpcr/${p.id}" }  // a Ct per well
    }
    steps: Channel<Record> {
        index {
            path 'steps.csv'  // one row per job: plate, protocol, capability, device, started_at, finished_at
            header true
        }
    }
    lab_report: Set<Path> {
        path 'lab_report'  // lab_report.json and .tsv: every job of this session, failed attempts too
    }
}
