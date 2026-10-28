package vagkaratzas.plugin

import spock.lang.Specification

class LabTaskHandlerTest extends Specification {

    def 'a task hands labctl its capability, queue and session; unset ones are removed' () {
        expect:
        LabTaskHandler.taskEnv(['lab/capability': 'centrifugation'], 'eppendorf', UUID.fromString('00000000-0000-0000-0000-000000000001')) ==
            [NF_LAB_CAPABILITY: 'centrifugation', NF_LAB_QUEUE: 'eppendorf', NF_LAB_SESSION: '00000000-0000-0000-0000-000000000001']
        LabTaskHandler.taskEnv([:], null, null) == [NF_LAB_CAPABILITY: null, NF_LAB_QUEUE: null, NF_LAB_SESSION: null]
        LabTaskHandler.taskEnv(['other/hint': 'x'], '', 'abc') == [NF_LAB_CAPABILITY: null, NF_LAB_QUEUE: null, NF_LAB_SESSION: 'abc']
    }

}
