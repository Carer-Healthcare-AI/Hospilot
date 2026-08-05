from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from config import settings
from workflows.graph.schedule_util import interval_from, validate_cron


class SignupRequest(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "doctor"
    org_id: str                       # organization the user is requesting to join

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        return v

    @field_validator("role")
    @classmethod
    def role_valid(cls, v: str) -> str:
        # super_admin is bootstrap-only (main.py), never self-assignable.
        if v not in ("doctor", "approver", "admin"):
            raise ValueError("Role must be one of: doctor, approver, admin")
        return v

    @field_validator("password")
    @classmethod
    def password_valid(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

    @field_validator("display_name")
    @classmethod
    def display_name_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Display name is required")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateSessionRequest(BaseModel):
    goal: str
    constraints: str = ""
    # Autonomous mode (Phase 3): plan + execute fully in the background with no
    # plan-approval wait. Default False keeps today's assisted behavior.
    autonomous: bool = False


class ExecuteSessionRequest(BaseModel):
    pipeline: dict
    agent_task_overrides: dict[str, list] = {}


class ApproveRequest(BaseModel):
    # Deprecated: the server now records the approver from the JWT (sub claim);
    # any value sent here is ignored. Kept optional for older frontends.
    approver_id: str | None = None
    override_vehicle_no: str | None = None
    decision: str


class OrgCreateRequest(BaseModel):
    name: str
    slug: str

    @field_validator("name")
    @classmethod
    def name_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Organization name is required")
        return v

    @field_validator("slug")
    @classmethod
    def slug_valid(cls, v: str) -> str:
        import re
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError("Slug must be lowercase letters, digits and hyphens")
        return v


class OrgUpdateRequest(BaseModel):
    name: str | None = None
    status: str | None = None         # active | disabled

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in ("active", "disabled"):
            raise ValueError("Status must be active or disabled")
        return v


class UserUpdateRequest(BaseModel):
    role: str | None = None           # doctor | approver (admin promotion is super_admin-only)
    status: str | None = None         # active | disabled


class ReorchestrationRequest(BaseModel):
    feedback: str | None = None
    agent_id: str | None = None       # absent -> pipeline reorchestration; present -> sub-agent or task level
    subagent_id: str | None = None    # present (with agent_id) -> task-level reorchestration


class UpdateSessionPipelineRequest(BaseModel):
    pipeline: dict


class EditResumeRequest(BaseModel):
    pipeline: dict                    # the edited execution pipeline to re-run
    checkpoint_id: str | None = None  # revert point; None -> latest paused checkpoint


class PlanDecisionRequest(BaseModel):
    action: str                       # "approve" | "edit" | "reorchestrate"
    pipeline: dict | None = None      # the edited pipeline (action == "edit")
    feedback: str | None = None       # guidance for replanning (action == "reorchestrate")


class IdentifyPatientRequest(BaseModel):
    mobiles: list[str]                # mobile number(s) of the incoming patient(s) to bind


class RenameSessionRequest(BaseModel):
    name: str = ""                    # display name (Workflows page); blank clears to null → "New Workflow"


# -- Scheduled recurring queries (Phase 6) -------------------------------------

def _resolve_cadence(
    every: int | None, unit: str | None, cron: str | None, timezone: str,
) -> tuple[str, int | None]:
    """Validate a cadence and return (schedule_kind, interval_seconds).

    Exactly one of (every + unit) or cron must be given. Intervals are floored at
    settings.scheduled_query_min_interval_seconds so a typo can't hammer the executor.
    """
    has_interval = every is not None or unit is not None
    has_cron = cron is not None
    if has_interval and has_cron:
        raise ValueError("provide either (every + unit) or cron, not both")
    if not has_interval and not has_cron:
        raise ValueError("provide a cadence: (every + unit) for a fixed interval, or cron")
    if has_cron:
        validate_cron(cron, timezone)
        return "cron", None
    if every is None or unit is None:
        raise ValueError("both 'every' and 'unit' are required for an interval schedule")
    secs = interval_from(every, unit)
    floor = settings.scheduled_query_min_interval_seconds
    if secs < floor:
        raise ValueError(
            f"interval must be at least {floor} seconds (~{floor // 60} min)")
    return "interval", secs


class CreateScheduledQueryRequest(BaseModel):
    """Register a saved query to re-run on a cadence. Provide EITHER (every + unit)
    for a fixed interval, OR cron for a calendar schedule."""
    goal: str
    constraints: str = ""
    name: str | None = None
    every: int | None = None                       # e.g. 6
    unit: Literal["minutes", "hours", "days"] | None = None  # e.g. "hours" -> every 6h
    cron: str | None = None                        # 5-field crontab, e.g. "0 2 * * *"
    timezone: str = "UTC"
    # Resolved by the validator from the cadence fields above:
    schedule_kind: str = ""
    interval_seconds: int | None = None

    @field_validator("goal")
    @classmethod
    def goal_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("goal is required")
        return v

    @model_validator(mode="after")
    def _resolve(self) -> "CreateScheduledQueryRequest":
        self.schedule_kind, self.interval_seconds = _resolve_cadence(
            self.every, self.unit, self.cron, self.timezone)
        return self


class UpdateScheduledQueryRequest(BaseModel):
    """Partial update: any subset of fields. Cadence is only re-validated/changed when
    a cadence field (every/unit/cron) is supplied; enabled toggles pause/resume."""
    goal: str | None = None
    constraints: str | None = None
    name: str | None = None
    enabled: bool | None = None
    every: int | None = None
    unit: Literal["minutes", "hours", "days"] | None = None
    cron: str | None = None
    timezone: str | None = None
    # Resolved when a cadence change was requested; None -> cadence unchanged:
    schedule_kind: str | None = None
    interval_seconds: int | None = None

    @model_validator(mode="after")
    def _resolve(self) -> "UpdateScheduledQueryRequest":
        if self.every is not None or self.unit is not None or self.cron is not None:
            self.schedule_kind, self.interval_seconds = _resolve_cadence(
                self.every, self.unit, self.cron, self.timezone or "UTC")
        return self


# -- A2A protocol schemas ------------------------------------------------------

class A2ATaskMeta(BaseModel):
    session_id: str
    context: dict = {}
    step: dict = {}
    remaining_plan: list = []


class A2ATaskParams(BaseModel):
    id: str
    message: dict = {}
    metadata: A2ATaskMeta


class A2ATaskRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str
    method: str = "tasks/send"
    params: A2ATaskParams


class A2ATaskResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: str
    result: dict
