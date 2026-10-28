"""wet-modules/: each module's meta.yml, script and result.schema.json agree, and every binding's result fits the schema."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.bindings.fake import FakeBackend
from nf_lab_adapter.bindings.plr import MACHINES
from test_adapter import req, wait, wells

MODULES = Path(__file__).parents[2] / "wet-modules" / "modules"
PROTOCOLS = sorted(p.name for p in MODULES.iterdir())


def meta(protocol: str) -> dict:
    """The `lab:` block's capability and protocol (no YAML parser in the adapter's environment)."""
    text = (MODULES / protocol / "meta.yml").read_text()
    return dict(re.findall(r"^  (capability|protocol): (\S+)", text, flags=re.M))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_meta_script_and_schema_agree(protocol):
    lab, script = meta(protocol), (MODULES / protocol / "main.nf").read_text()
    assert lab["protocol"] == protocol
    assert f"--protocol {protocol} " in script
    assert f"hints 'lab/capability': '{lab['capability']}'" in script
    Draft202012Validator.check_schema(json.loads((MODULES / protocol / "result.schema.json").read_text()))


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("binding", ["fake", "plr"])
def test_a_binding_result_fits_the_schema(tmp_path, protocol, binding):
    capability = meta(protocol)["capability"]
    backend = FakeBackend(0.0) if binding == "fake" else MACHINES[capability](0.0)
    schema = json.loads((MODULES / protocol / "result.schema.json").read_text())
    plate = wells("A1:S1", "B1:S2", "C1:NC1:negative_control", "D1:BLK1:blank", "E1:POS1:positive_control")
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), [Device("d-1", capability, backend, simulated_operator=True)])) as c:
        job = wait(c, c.post("/jobs", json={**req("k", protocol), "capability": capability, "wells": plate}).json()["job_id"])
        assert job["state"] == "done", job
        Draft202012Validator(schema).validate(job["result"])
