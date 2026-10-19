package vagkaratzas.plugin

import groovy.transform.CompileStatic
import groovy.util.logging.Slf4j
import nextflow.executor.local.LocalExecutor
import nextflow.processor.TaskHandler
import nextflow.processor.TaskRun
import nextflow.util.ServiceName
import org.pf4j.ExtensionPoint

/**
 * Lab executor, shape B: the task wrapper runs on the controller host,
 * and the task script drives the device through `labctl`.
 *
 * plain LocalExecutor for now; TODO return a lab handler when
 * kill() must become safe-stop (day 3/4).
 */
@Slf4j
@CompileStatic
@ServiceName('lab')
class LabExecutor extends LocalExecutor implements ExtensionPoint {

    @Override
    TaskHandler createTaskHandler(TaskRun task) {
        // Per-task device settings arrive as `hints 'lab/<key>': ...` (day 6 binds capability -> device).
        log.debug "[lab] ${task.name} hints=${task.config.getHints()}"
        return super.createTaskHandler(task)
    }
}
