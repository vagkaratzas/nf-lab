package vagkaratzas.plugin

import groovy.transform.CompileStatic
import nextflow.executor.local.LocalTaskHandler
import nextflow.processor.TaskRun

/**
 * A local task whose wrapper starts with the `lab {}` settings in its environment.
 * The launch environment is not written to the work dir and not in the task hash:
 * a new adapter URL doesn't re-run a task.
 */
@CompileStatic
class LabTaskHandler extends LocalTaskHandler {

    private final Map<String,String> labEnv

    LabTaskHandler(TaskRun task, LabExecutor executor, Map<String,String> labEnv) {
        super(task, executor)
        this.labEnv = labEnv
    }

    @Override
    protected ProcessBuilder createLaunchProcessBuilder() {
        final pb = super.createLaunchProcessBuilder()
        pb.environment().putAll(labEnv)
        return pb
    }
}
