nextflow.enable.types = true

// Closing dry step, like MultiQC at the end of an nf-core pipeline: the lab's side of this
// run. Every job this session submitted or collected (failed attempts and a person's
// resolves too), with the tasks that collected it, each a lineage id (lid://<task hash>).
// As a declared output, it puts the device ledger's history into Nextflow's lineage.
// Its input is every wet task's provenance, so it runs last and again whenever one changes.
process LAB_REPORT {
    label 'process_lab'  // dry, but asks the adapter: gets its token

    input:
    provenance: Bag<Path>

    output:
    files('lab_report.*')

    script:
    """
    labctl report --session ${workflow.sessionId}
    """
}
