"""Device-job contract: the request and reply shapes every adapter must serve.

The exported OpenAPI spec (adapter/openapi.json) is generated from these models
and is the contract other adapter implementations follow.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)


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
    positive_control = "positive_control"
    blank = "blank"


class Well(BaseModel):
    """One filled well of the plate map. Row and column cover plates up to 384 wells;
    the backend binding checks they exist on its labware."""

    row: str = Field(pattern=r"^[A-P]$")
    column: int = Field(ge=1, le=24)
    sample: str = Field(min_length=1, description="Sample ID, or the control's or blank's own ID")
    type: WellType = WellType.sample


class JobSpec(BaseModel):
    """What the job is: the payload behind the idempotency key, plus routing."""

    capability: str = Field(min_length=1, examples=["liquid-handling"])
    queue: str | None = Field(
        None, min_length=1, examples=["opentrons"],
        description="Only the capability's devices in this queue may take the job (Nextflow's `queue` directive); "
                    "None: any of them. Routing, not identity: outside the idempotency key")
    protocol: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    plate: str = Field(min_length=1, description="Plate ID from the plate map")
    wells: list[Well] = Field(min_length=1, description="One per filled well; kept in row, column order")
    params: dict[str, StrictBool | StrictInt | StrictFloat | StrictStr] = Field(
        default_factory=dict, examples=[{"g": 500.0, "duration_s": 120}],
        description="The protocol's parameters (its module's input.schema.json): what the pipeline asks for. "
                    "A param not given runs at the lab's or the binding's default. Unknown, wrong-typed or "
                    "out-of-range params are refused (422). Part of the idempotency key")
    idempotency_key: str = Field(
        min_length=1, description="hash(protocol, version, plate, wells in row, column order[, params by name]); "
                                  "one physical job"
    )

    @field_validator("wells")
    @classmethod
    def one_sample_per_well(cls, wells: list[Well]) -> list[Well]:
        wells = sorted(wells, key=lambda w: (w.row, w.column))  # the order wells are listed in carries no meaning
        for a, b in zip(wells, wells[1:]):
            if (a.row, a.column) == (b.row, b.column):
                raise ValueError(f"well {a.row}{a.column} listed twice")
        return wells


class SubmitRequest(JobSpec):
    task_hash: str | None = Field(
        None, pattern=r"^[0-9a-f]{32}$",
        description="The Nextflow task submitting (or collecting) the job: its hash, i.e. its work dir and its "
                    "lineage id (lid://<hash>). Kept per job, outside the idempotency key")
    session_id: str | None = Field(
        None, min_length=1, description="That task's Nextflow session (`-resume` keeps it): GET /jobs?session= lists "
                                        "every job it collected, failed attempts too")


class Job(JobSpec):
    job_id: str
    attempt: int = Field(description="1 + number of earlier attempts for this key (failed pre-flight or voided)")
    device_id: str | None = Field(
        description="The device that took the job; None while queued: the free device of the capability "
                    "whose deck holds the plate takes it"
    )
    program: dict | None = Field(
        description="What the protocol runs with: the binding's defaults, then the lab's, then `params`. "
                    "Resolved when the job is queued; outside the idempotency key")
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


class Collection(BaseModel):
    """A Nextflow task that submitted or collected the job (a retry, `-resume` or a fresh launch adds one)."""

    task_hash: str = Field(description="Its work dir and lineage id: lid://<task_hash>")
    session_id: str | None
    collected_at: datetime


class CollectedJob(Job):
    collected_by: list[Collection] = Field(description="Every Nextflow task that submitted or collected this job")
