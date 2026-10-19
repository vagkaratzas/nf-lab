"""Device-job contract: the request and reply shapes every adapter must serve.

The exported OpenAPI spec (adapter/openapi.json) is generated from these models
and is the contract other adapter implementations follow.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class State(StrEnum):
    queued = "queued"
    preflight = "preflight"
    running = "running"
    done = "done"
    failed_preflight = "failed_preflight"  # nothing consumed: retryable
    failed_in_protocol = "failed_in_protocol"  # material touched: never retried
    stopped = "stopped"  # a human stopped the device (e-stop, vendor UI)


FINAL = {State.done, State.failed_preflight, State.failed_in_protocol, State.stopped}


class SubmitRequest(BaseModel):
    capability: str = Field(min_length=1, examples=["liquid-handling"])
    protocol: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    sample_ids: list[str] = Field(min_length=1)
    idempotency_key: str = Field(
        min_length=1, description="hash(protocol version + sample IDs); one physical job"
    )


class Job(SubmitRequest):
    job_id: str
    attempt: int = Field(description="1 + number of earlier pre-flight failures for this key")
    device_id: str
    state: State
    stoppable: bool = Field(description="True until the first liquid moves (the stop boundary)")
    stop_requested: bool = Field(description="safe_stop arrived after the stop boundary")
    reason: str | None = Field(description="Why the job failed or stopped")
    result: dict | None = Field(description="Backend outputs, kept so a repeated submit can return them")
    submitted_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    released_at: datetime | None
    device_heartbeat_at: datetime | None = Field(
        description="Last time the device itself reported; stale = silent device"
    )
