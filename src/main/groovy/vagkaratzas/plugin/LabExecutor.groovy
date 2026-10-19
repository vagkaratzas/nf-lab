package vagkaratzas.plugin

import groovy.transform.CompileStatic
import groovy.util.logging.Slf4j
import nextflow.executor.local.LocalExecutor
import nextflow.util.ServiceName
import org.pf4j.ExtensionPoint

/**
 * Lab executor, shape B: the task wrapper runs on the controller host,
 * and the task script drives the device through `labctl`.
 *
 * ponytail: plain LocalExecutor for now; override createTaskHandler when
 * kill() must become safe-stop (day 3/4).
 */
@Slf4j
@CompileStatic
@ServiceName('lab')
class LabExecutor extends LocalExecutor implements ExtensionPoint {
}
