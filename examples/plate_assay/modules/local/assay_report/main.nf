nextflow.enable.types = true

// Dry step: one TSV row per read well: sample, type, plate, well, absorbance, and the
// absorbance minus the plate's mean blank (0 when the plate has no blanks).
process ASSAY_REPORT {
    tag "${id}"

    input:
    record(
        id: String,
        result: Path
    )

    output:
    file("${id}.assay.tsv")

    script:
    """
    jq -r '([.wells[] | select(.type == "blank") | .absorbance] | if length > 0 then add / length else 0 end) as \$blank
        | .wells[] | [.sample, .type, "${id}", "\\(.row)\\(.column)", .absorbance, ((.absorbance - \$blank) * 1000 | round / 1000)] | @tsv' \\
        ${result} > ${id}.assay.tsv
    """
}
