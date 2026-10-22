"""Device-job contract: the request and reply shapes every adapter must serve.

The exported OpenAPI spec (adapter/openapi.json) is generated from these models
and is the contract other adapter implementations follow.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class State(StrEnum):
    queued = "queued"
    preflight = "preflight"
    running = "running"
    done = "done"
    failed_preflight = "failed_preflight"  # nothing consumed: retryable
    failed_in_protocol = "failed_in_protocol"  # material touched: never retried
    stopped = "stopped"  # a human stopped the device (e-stop, vendor UI)
    voided = "voided"  # a human threw the material away: the key may run again


FINAL = {State.done, State.failed_preflight, State.failed_in_protocol, State.stopped, State.voided}


class WellType(StrEnum):
    sample = "sample"
    negative_control = "negative_control"
    blank = "blank"


class Well(BaseModel):
    """One filled well of the plate map. Row and column cover plates up to 384 wells;
    the backend binding checks they exist on its labware."""

    row: str = Field(pattern=r"^[A-P]$")
    column: int = Field(ge=1, le=24)
    sample: str = Field(min_length=1, description="Sample ID, or the control's or blank's own ID")
    type: WellType = WellType.sample


class SubmitRequest(BaseModel):
    capability: str = Field(min_length=1, examples=["liquid-handling"])
    queue: str | None = Field(
        None, min_length=1, examples=["opentrons"],
        description="Only the capability's devices in this queue may take the job (Nextflow's `queue` directive); "
                    "None: any of them. Routing, not identity: outside the idempotency key")
    protocol: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    plate: str = Field(min_length=1, description="Plate ID from the plate map")
    wells: list[Well] = Field(min_length=1, description="One per filled well; kept in row, column order")
    idempotency_key: str = Field(
        min_length=1, description="hash(protocol, version, plate, wells in row, column order); one physical job"
    )

    @field_validator("wells")
    @classmethod
    def one_sample_per_well(cls, wells: list[Well]) -> list[Well]:
        wells = sorted(wells, key=lambda w: (w.row, w.column))  # the order wells are listed in carries no meaning
        for a, b in zip(wells, wells[1:]):
            if (a.row, a.column) == (b.row, b.column):
                raise ValueError(f"well {a.row}{a.column} listed twice")
        return wells


class Job(SubmitRequest):
    job_id: str
    attempt: int = Field(description="1 + number of earlier attempts for this key (failed pre-flight or voided)")
    device_id: str | None = Field(
        description="The device that took the job; None while queued: the free device of the capability "
                    "whose deck holds the plate takes it"
    )
    state: State
    stoppable: bool = Field(description="True until the first liquid moves (the stop boundary)")
    stop_requested: bool = Field(description="safe_stop arrived after the stop boundary")
    reason: str | None = Field(description="Why the job failed or stopped; who resolved or voided it, and why")
    result: dict | None = Field(description="Backend outputs, kept so a repeated submit can return them")
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    released_at: datetime | None
    device_heartbeat_at: datetime | None = Field(
        description="Last time the device itself reported; stale = silent device"
    )
