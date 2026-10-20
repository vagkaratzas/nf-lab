nextflow.enable.types = true

// Dry step: one TSV row per sample, from the wet step's result and provenance.
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
        '. as \$r | \$r.sample_ids[] | [., "${id}", \$r.wells[.], \$r.volume_ul[.], \$p[0].device_id, \$p[0].job_id, \$p[0].protocol_version] | @tsv' \\
        ${result} > ${id}.report.tsv
    """
}
