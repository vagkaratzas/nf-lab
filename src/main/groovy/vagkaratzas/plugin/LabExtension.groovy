/*
 * Copyright 2025, Seqera Labs
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package vagkaratzas.plugin

import java.nio.file.Files
import java.nio.file.Path

import groovy.json.JsonSlurper
import groovy.transform.CompileStatic
import nextflow.Session
import nextflow.plugin.extension.Function
import nextflow.plugin.extension.PluginExtensionPoint

/**
 * Functions a pipeline imports with `include { labPlate } from 'plugin/nf-lab'`.
 */
@CompileStatic
class LabExtension extends PluginExtensionPoint {

    @Override
    protected void init(Session session) {
    }

    /**
     * A wet task's plate as plain data, for the pipeline to build its own record from:
     * the job fields of `provenance.json` (device_id, job_id, attempt, reason, ...)
     * with the fields of `result.json` on top (plate, and its wells with what was measured).
     */
    @Function
    Map<String,Object> labPlate(Path provenance, Path result) {
        return labPlate(provenance) + readJson(result)
    }

    /**
     * A wet task's plate from its provenance alone: the job fields and the wells it was given.
     */
    @Function
    Map<String,Object> labPlate(Path provenance) {
        return readJson(provenance)
    }

    private static Map<String,Object> readJson(Path path) {
        return Files.newBufferedReader(path).withCloseable { reader -> new JsonSlurper().parse(reader) as Map<String,Object> }
    }

}
