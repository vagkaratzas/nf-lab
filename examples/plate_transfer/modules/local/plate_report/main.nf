nextflow.enable.types = true

// Dry step: one TSV row per filled well (controls, blanks and wells read at 0 µL
// included), from the wet step's result and provenance; the last two columns say
// how the job ended (attempt, and the reason if a person resolved it).
process PLATE_REPORT {
    tag "${id}"

    input:
    record(
        id: String,
        result: Path,
        provenance: Path
    )

    output:
    file("${id}.report.tsv")

    script:
    """
    jq -r --slurpfile p ${provenance} \\
        '.wells[] | [.sample, .type, "${id}", "\\(.row)\\(.column)", .volume_ul, \$p[0].device_id, \$p[0].job_id, \$p[0].protocol_version, \$p[0].attempt, (\$p[0].reason // "")] | @tsv' \\
        ${result} > ${id}.report.tsv
    """
}
