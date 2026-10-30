package vagkaratzas.plugin

import groovy.transform.CompileStatic
import nextflow.config.spec.ConfigOption
import nextflow.config.spec.ConfigScope
import nextflow.config.spec.ScopeName
import nextflow.script.dsl.Description
import nextflow.util.Duration

/**
 * The run's own lab settings. The lab executor hands them to `labctl` as
 * NF_LAB_* environment variables; an unset one falls back to the launch shell's.
 * The lab's settings (robots, ledger, backend) stay with the adapter.
 */
@ScopeName('labrobotics')
@Description('''
    The `labrobotics` scope sets how this run's wet tasks reach the lab's device adapter.
''')
@CompileStatic
class LabConfig implements ConfigScope {

    @ConfigOption
    @Description('''
        URL of the device adapter (default: `NF_LAB_ADAPTER` from the launch environment, else `http://127.0.0.1:8000`).
    ''')
    final String adapter

    @ConfigOption
    @Description('''
        Time between `labctl` status polls (default: `2s`).
    ''')
    final Duration poll

    @ConfigOption
    @Description('''
        How long `labctl` absorbs an unreachable adapter before it exits 69, outcome unknown (default: `5m`).
    ''')
    final Duration grace

    // no-arg constructor is required to enable validation of config options
    LabConfig() {
    }

    LabConfig(Map opts) {
        adapter = opts.adapter as String
        poll = opts.poll as Duration
        grace = opts.grace as Duration
    }

    /** The settings as labctl's environment: only those set. */
    Map<String,String> toEnv() {
        final env = new LinkedHashMap<String,String>()
        if( adapter ) env.NF_LAB_ADAPTER = adapter
        if( poll ) env.NF_LAB_POLL = seconds(poll)
        if( grace ) env.NF_LAB_GRACE = seconds(grace)
        return env
    }

    private static String seconds(Duration d) {
        return (d.toMillis() / 1000).toString()
    }
}
