package vagkaratzas.plugin

import nextflow.util.Duration
import spock.lang.Specification

class LabConfigTest extends Specification {

    def 'only the settings a run sets reach labctl, durations in seconds' () {
        expect:
        new LabConfig([:]).toEnv() == [:]
        new LabConfig(adapter: 'http://lab:8000', poll: '500ms', grace: Duration.of('5m')).toEnv() ==
            [NF_LAB_ADAPTER: 'http://lab:8000', NF_LAB_POLL: '0.5', NF_LAB_GRACE: '300']
    }

}
