nextflow.enable.types = true

// Dry step: one TSV row per extracted well: sample, type, analyte, plate, well, eluate_ul,
// yield_ng_ul and a QC call. A sample passes at min_yield ng/µL or more; a control or a
// blank passes only with no yield (anything else is contamination).
process YIELD_QC {
    tag "${id}"

    input:
    record(
        id: String,
        result: Path
    )
    min_yield: Float

    output:
    file("${id}.yield_qc.tsv")

    script:
    """
    jq -r '.analyte as \$a | .wells[]
        | [.sample, .type, \$a, "${id}", "\\(.row)\\(.column)", .eluate_ul, .yield_ng_ul,
           (if .type == "sample" then (if .yield_ng_ul >= ${min_yield} then "pass" else "low_yield" end)
            else (if .yield_ng_ul == 0 then "pass" else "contaminated" end) end)] | @tsv' \\
        ${result} > ${id}.yield_qc.tsv
    """
}
