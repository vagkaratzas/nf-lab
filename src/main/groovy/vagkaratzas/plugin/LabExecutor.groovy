package vagkaratzas.plugin

import groovy.transform.CompileStatic
import groovy.util.logging.Slf4j
import nextflow.executor.local.LocalExecutor
import nextflow.processor.TaskHandler
import nextflow.processor.TaskRun
import nextflow.script.ScriptType
import nextflow.util.ServiceName
import org.pf4j.ExtensionPoint

/**
 * Lab executor, shape B: the task wrapper runs on the controller host,
 * and the task script drives the device through `labctl`.
 * It hands the run's `lab {}` settings to `labctl` as environment variables.
 */
@Slf4j
@CompileStatic
@ServiceName('lab')
class LabExecutor extends LocalExecutor implements ExtensionPoint {

    private Map<String,String> labEnv

    @Override
    protected void register() {
        super.register()
        labEnv = new LabConfig(session.config.navigate('lab') as Map ?: [:]).toEnv()
        log.debug "[lab] settings from config: ${labEnv}"
    }

    @Override
    TaskHandler createTaskHandler(TaskRun task) {
        if( task.type == ScriptType.GROOVY )  // exec: tasks run in the JVM, no wrapper to hand settings to
            return super.createTaskHandler(task)
        return new LabTaskHandler(task, this, labEnv)
    }
}
