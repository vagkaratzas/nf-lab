package vagkaratzas.plugin

import groovy.transform.CompileStatic
import nextflow.executor.local.LocalTaskHandler
import nextflow.processor.TaskRun

/**
 * A local task whose wrapper starts with the `lab {}` settings and the task's own routing in its
 * environment, for `labctl`: the capability (the `lab/capability` hint), the queue (the `queue`
 * directive) and the Nextflow session. So a wet module names none of them in its script.
 * The launch environment is not written to the work dir and not in the task hash: a new adapter
 * URL or queue doesn't re-run a task (neither did the hint or the queue in the script).
 */
@CompileStatic
class LabTaskHandler extends LocalTaskHandler {

    private final Map<String,String> labEnv

    LabTaskHandler(TaskRun task, LabExecutor executor, Map<String,String> labEnv) {
        super(task, executor)
        this.labEnv = labEnv
    }

    /**
     * The task's routing for labctl. A null value means unset: the launch removes it, so a variable
     * left in the launch shell never routes a task its directives didn't ask for.
     */
    static Map<String,String> taskEnv(Map<String,Object> hints, Object queue, Object session) {
        return [
            NF_LAB_CAPABILITY: hints?.get('lab/capability')?.toString(),
            NF_LAB_QUEUE: queue?.toString() ?: null,
            NF_LAB_SESSION: session?.toString(),
        ]
    }

    @Override
    protected ProcessBuilder createLaunchProcessBuilder() {
        final pb = super.createLaunchProcessBuilder()
        final env = pb.environment()
        env.putAll(labEnv)
        taskEnv(task.config.getHints(), task.config.get('queue'), task.processor?.session?.uniqueId).each { k, v ->
            if( v == null ) env.remove(k) else env.put(k, v)
        }
        return pb
    }
}
