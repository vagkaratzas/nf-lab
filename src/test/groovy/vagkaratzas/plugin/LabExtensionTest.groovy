package vagkaratzas.plugin

import java.nio.file.Files

import spock.lang.Specification

class LabExtensionTest extends Specification {

    def 'labPlate merges the job fields with the result, result on top' () {
        given:
        def dir = Files.createTempDirectory('lab-plate')
        def provenance = dir.resolve('provenance.json')
        def result = dir.resolve('result.json')
        provenance.text = '{"plate": "P1", "device_id": "ot-1", "attempt": 2, "reason": null, "wells": [{"row": "A", "column": 1}]}'
        result.text = '{"plate": "P1", "wells": [{"row": "A", "column": 1, "volume_ul": 150.0}]}'
        def ext = new LabExtension()

        when:
        def plate = ext.labPlate(provenance, result)
        then:
        plate.plate == 'P1'
        plate.device_id == 'ot-1'
        plate.attempt == 2
        plate.reason == null
        plate.wells == [[row: 'A', column: 1, volume_ul: 150.0]]

        expect:
        ext.labPlate(provenance).wells == [[row: 'A', column: 1]]

        cleanup:
        dir.deleteDir()
    }

}
