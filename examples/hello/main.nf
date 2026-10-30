// Hello pipeline: one wet step on the `labrobotics` executor, one dry step on `local`.

process WET {
    label 'process_wet'
    hints 'labrobotics/capability': 'liquid-handling'

    input:
    val sample

    output:
    path 'plate.txt'

    script:
    """
    echo "plate for ${sample}" > plate.txt
    """
}

process DRY {
    input:
    path plate

    output:
    stdout

    script:
    """
    cat ${plate}
    """
}

workflow {
    channel.of('s1', 's2') | WET | DRY | view
}
