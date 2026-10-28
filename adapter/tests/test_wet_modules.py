"""wet-modules/: each module's meta.yml, script, input.schema.json and result.schema.json agree with the bindings:
every binding takes the params the module declares, and its result fits the schema."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from nf_lab_adapter.app import Device, create_app
from nf_lab_adapter.bindings import NONBLANK, PARAMS, resolve
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
    assert "--params - <<'JSON'" in script and "JsonOutput.toJson(task.ext.params)" in script  # every param reaches the adapter
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


TYPES = {float: "number", int: "integer", str: "string"}


def inputs(protocol: str) -> dict:
    return json.loads((MODULES / protocol / "input.schema.json").read_text())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_input_schema_declares_the_bindings_params(protocol):
    schema = inputs(protocol)
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    declared = {name: prop["type"] for name, prop in schema["properties"].items()}
    assert declared == {name: TYPES[p.type] for name, p in PARAMS[protocol].items()}
    for name, prop in schema["properties"].items():
        if prop["type"] == "string":  # non-blank: the resolver's rule, the same text
            assert (prop["minLength"], prop["pattern"]) == (1, NONBLANK)


@pytest.mark.parametrize("value", ["", "   ", "\u0085", "\u00a0", "DMSO"])
def test_the_schema_and_the_resolver_agree_on_a_blank_string(value):
    schema_ok = Draft202012Validator(inputs("reagent_dispense")).is_valid({"reagent": value})
    try:
        resolve("reagent_dispense", {"reagent": value})
        resolver_ok = True
    except ValueError:
        resolver_ok = False
    assert schema_ok == resolver_ok == (value not in ("", "   "))


def other(p) -> float | int | str:
    """A valid value that isn't the binding's default."""
    if p.type is str:
        return next((c for c in p.choices if c != p.default), None) if p.choices else f"not {p.default}"
    value = (p.default + p.max) / 2 if p.type is float else (p.default + p.max) // 2
    return value if value != p.default else p.default - 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("binding", ["fake", "plr"])
def test_a_binding_runs_every_param_it_declares_and_refuses_others(tmp_path, protocol, binding):
    capability = meta(protocol)["capability"]
    backend = FakeBackend(0.0) if binding == "fake" else MACHINES[capability](0.0)
    params = {name: other(p) for name, p in PARAMS[protocol].items()}
    Draft202012Validator(inputs(protocol)).validate(params)
    schema = json.loads((MODULES / protocol / "result.schema.json").read_text())
    plate = wells("A1:S1", "B1:S2", "C1:NC1:negative_control", "D1:BLK1:blank", "E1:POS1:positive_control")
    with TestClient(create_app(str(tmp_path / "ledger.sqlite"), [Device("d-1", capability, backend, simulated_operator=True)])) as c:
        body = {**req("k", protocol), "capability": capability, "wells": plate}
        assert c.post("/jobs", json={**body, "idempotency_key": "x", "params": {"no_such_param": 1}}).status_code == 422
        job = wait(c, c.post("/jobs", json={**body, "params": params}).json()["job_id"], timeout=15)
        assert job["state"] == "done", job
        assert job["program"] == params
        Draft202012Validator(schema).validate(job["result"])
        if "program" in job["result"]:
            assert job["result"]["program"] == params
