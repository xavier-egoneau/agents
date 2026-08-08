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
    vision: bool = False

    # A llama.cpp provider with ``models_dir`` is managed by AMK. Providers
    # with only ``base_url``/``port`` remain compatible with external servers.
    models_dir: str | None = None
    server_binary: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    n_gpu_layers: int | None = None
    num_ctx: int | None = Field(default=None, gt=0)
    threads: int | None = Field(default=None, gt=0)
    parallel: int | None = Field(default=None, gt=0)
    batch_size: int | None = Field(default=None, gt=0)
    ubatch_size: int | None = Field(default=None, gt=0)
    flash_attn: bool | None = None
    startup_timeout_seconds: int | None = Field(default=None, gt=0, le=1800)
    llama_args: list[str] = Field(default_factory=list)
    temperature: float | None = Field(default=None, ge=0)
    top_k: int | None = Field(default=None, ge=0)
    top_p: float | None = Field(default=None, ge=0, le=1)
    num_predict: int | None = Field(default=None, gt=0)

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
    user_memory: bool = False
    declared_tools: list[str] = Field(default_factory=list)
    # Deux niveaux stricts : un orchestrateur délègue à des sous-agents, un
    # sous-agent exécute et ne délègue à personne.
    #
    # `None` signifie « non déclaré », et se distingue de `False`. Ce champ est
    # arrivé après les agents : exiger sa présence rendait invalides des
    # configurations qui fonctionnaient, et le socle ne peut pas le rétro-ajouter
    # aux fichiers que l'utilisateur a modifiés. Le niveau est alors déduit de
    # la place réelle de l'agent dans le graphe.
    subagent: bool | None = None
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
    path_parameters: list[str] = Field(default_factory=list)
    url_parameters: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    cancellable: bool = False
    persistent: bool = False


class ModuleConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    type: Literal[
        "text", "secret", "file", "directory", "integer", "number",
        "boolean", "select", "string_list",
    ]
    description: str | None = None
    required: bool = False
    default: Any = None
    secret_name: str | None = None
    options: list[str] = Field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_field_contract(self) -> ModuleConfigField:
        if self.type == "secret" and not self.secret_name:
            raise ValueError("secret fields require secret_name")
        if self.type != "secret" and self.secret_name:
            raise ValueError("secret_name is reserved for secret fields")
        if self.type == "select" and not self.options:
            raise ValueError("select fields require options")
        return self


class ModuleConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    applies_to: list[str] = Field(default_factory=list)
    fields: list[ModuleConfigField] = Field(min_length=1)
    legacy_file: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._-]+\.json$")

    @model_validator(mode="after")
    def validate_unique_fields(self) -> ModuleConfiguration:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("module configuration field names must be unique")
        return self


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
    config: ModuleConfiguration | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_configuration_capability(self) -> ModuleManifest:
        configurable = "config" in self.capabilities
        if configurable != (self.config is not None):
            raise ValueError("capability `config` and the config schema must be declared together")
        if self.config:
            tools = {tool.name for tool in self.tools}
            unknown = set(self.config.applies_to) - tools
            if unknown:
                raise ValueError(f"config applies_to references unknown tools: {sorted(unknown)}")
        return self


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
    tool_description: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
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
    trigger: Literal[
        "user", "resume", "cron", "cron_resume", "cron_test", "telegram"
    ] = "user"
    hidden: bool = False
    cron_job_id: str | None = None
    cron_occurrence_id: str | None = None
    workflow: dict[str, Any] | None = None
    # ``None`` preserves the historical unrestricted tool selection. An empty
    # list deliberately exposes no module tool, while a non-empty list is an
    # exact runtime allowlist.
    tool_allowlist: list[str] | None = None
    # La bibliothèque n'entre jamais dans le contexte d'elle-même : `off` est le
    # défaut. `auto` fournit l'index pour que l'agent sache quoi chercher —
    # sans lui il ignore que la bibliothèque contient quelque chose. `manual`
    # injecte les pages explicitement retenues.
    knowledge_mode: Literal["off", "auto", "manual"] = "off"
    knowledge_pages: list[str] = Field(default_factory=list)


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
