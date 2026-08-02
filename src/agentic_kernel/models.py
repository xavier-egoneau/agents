from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConnectionType(StrEnum):
    LOCAL = "local"
    API_KEY = "api_key"
    AUTH = "auth"


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_depth: int = Field(default=5, ge=1, le=20)
    max_agent_runs: int = Field(default=24, ge=1, le=256)
    max_concurrency: int = Field(default=6, ge=1, le=64)
    # A tool-heavy implementation run can legitimately require dozens of
    # model/tool round trips. This limit applies to one run, not to the number
    # of messages that a durable session may contain.
    max_requests_per_agent: int = Field(default=100, ge=1, le=500)
    session_timeout_seconds: float = Field(default=1800, gt=0, le=86400)
    child_timeout_seconds: float = Field(default=600, gt=0, le=86400)
    retries: int = Field(default=2, ge=0, le=10)


class ProviderConfig(BaseModel):
    """Normalized provider config; legacy fields are deliberately tolerated."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: str
    connection_type: ConnectionType | None = None
    base_url: str | None = None
    # OAuth providers discover their models after authentication. A model is
    # selected per agent/run rather than being required in the provider file.
    model: str | None = None
    models: list[str] = Field(default_factory=list)
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=120, gt=0)

    @model_validator(mode="after")
    def infer_connection(self) -> ProviderConfig:
        if self.connection_type is None:
            if self.kind in {"ollama", "llama-cpp", "llama.cpp"}:
                self.connection_type = ConnectionType.LOCAL
            elif self.kind in {"openai-codex", "anthropic-oauth", "claude-oauth"}:
                self.connection_type = ConnectionType.AUTH
            else:
                self.connection_type = ConnectionType.API_KEY
        return self


class ProviderRegistry(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: int = 1
    default_provider: str
    providers: list[ProviderConfig]

    @model_validator(mode="after")
    def validate_registry(self) -> ProviderRegistry:
        ids = [provider.id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("provider ids must be unique")
        if self.default_provider not in ids:
            raise ValueError("default_provider must reference a configured provider")
        return self


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str
    provider: str
    model: str | None = None
    modules: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    declared_tools: list[str] = Field(default_factory=list)
    delegates: list[str] = Field(default_factory=list)
    budgets: BudgetConfig | None = None
    instructions: str
    source: str


class SkillConfig(BaseModel):
    """Normalized Agent Skill compatible with OpenAI and Claude SKILL.md files."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    description: str
    instructions: str
    source: str
    root: str
    allowed_tools: list[str] = Field(default_factory=list)


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    NETWORK = "network"
    SECRET = "secret"
    EXTERNAL = "external"
    SYSTEM = "system"
    EXECUTE = "execute"
    SCREEN = "screen"


class ToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    description: str
    category: str = "general"
    risk_tags: list[ToolRisk] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    cancellable: bool = False
    persistent: bool = False


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Framework-independent result contract shared by every executable tool."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Any = None
    error: ToolError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_error_state(self) -> ToolResult:
        if self.ok and self.error is not None:
            raise ValueError("a successful tool result cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("a failed tool result must contain an error")
        return self


class ModuleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str
    description: str
    version: str
    entrypoint: str
    capabilities: list[Literal["tools", "instructions", "hooks", "config"]]
    tools: list[ToolDescriptor] = Field(default_factory=list)
    enabled: bool = True


class ModuleIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    modules: list[ModuleManifest]


class RunStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    TIMEOUT = "timeout"
    APPROVAL_PENDING = "approval_pending"
    CANCELLED = "cancelled"


class SecurityMode(StrEnum):
    SAFE = "safe"
    LIMITED = "limited"
    POWER = "power"


class GuardianVerdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class GuardianDecision(BaseModel):
    verdict: GuardianVerdict
    reason: str
    tool_name: str
    agent_id: str
    tool_call_id: str
    path: str | None = None
    risks: list[ToolRisk] = Field(default_factory=list)
    justification: str
    security_mode: SecurityMode


class ApprovalRequest(BaseModel):
    approval_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    run_id: UUID
    agent_id: str
    tool_call_id: str
    tool_name: str
    action_family: str
    path: str | None = None
    justification: str
    risks: list[ToolRisk] = Field(default_factory=list)
    reason: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved: bool | None = None


class ApprovalResolution(BaseModel):
    approval_id: UUID
    approved: bool
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ToolTrace(BaseModel):
    tool_call_id: str
    tool_name: str
    phase: str
    path: str | None = None
    duration_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ImageAttachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data_base64: str = Field(min_length=1)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    agent_id: str = "main"
    skills: list[str] = Field(default_factory=list)
    session_id: UUID = Field(default_factory=uuid4)
    budgets: BudgetConfig | None = None
    workspace: Path | None = None
    security_mode: SecurityMode = SecurityMode.LIMITED
    provider_id: str | None = None
    model: str | None = None
    reasoning: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    images: list[ImageAttachment] = Field(default_factory=list)
    trigger: Literal["user", "resume", "cron", "cron_resume", "cron_test"] = "user"
    cron_job_id: str | None = None
    cron_occurrence_id: str | None = None


class RunError(BaseModel):
    type: str
    message: str
    retryable: bool = False
    attempt: int = 0


class RunArtifact(BaseModel):
    artifact_id: str
    name: str
    media_type: str
    kind: str = "file"
    bytes: int | None = None


class RunResult(BaseModel):
    session_id: UUID
    run_id: UUID
    agent_id: str
    status: RunStatus
    output: str | None = None
    errors: list[RunError] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[RunArtifact] = Field(default_factory=list)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    session_id: UUID
    run_id: UUID
    agent_id: str
    parent_run_id: UUID | None = None
    type: str
    attempt: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)
