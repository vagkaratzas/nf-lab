nextflow.enable.types = true

// Wet + dry: each plate's samples go through a simulated liquid handler
// (PLATE_TRANSFER, lab executor), then a report (PLATE_REPORT, local).

include { PLATE_TRANSFER } from './modules/local/plate_transfer'
include { PLATE_REPORT } from './modules/local/plate_report'

params {
    samples: Channel<Sample>  // CSV with columns sample,plate
}

record Sample {
    sample: String
    plate: String
}

workflow {
    main:
    plates = params.samples
        .map { s -> tuple(s.plate, s.sample) }
        .groupBy()
        // groupBy gives an unordered Bag; sort so the key and the task hash are stable
        .map { plate, samples -> record(id: plate, samples: samples.toSorted()) }
    transferred = PLATE_TRANSFER(plates)
    reports = PLATE_REPORT(transferred)

    publish:
    plates = transferred
    reports = reports
}

output {
    plates: Channel<Record> {
        path { p -> "plates/${p.id}" }  // result.json, provenance.json, labctl.log
    }
    reports: Channel<Path> {
        path 'reports'
    }
}
