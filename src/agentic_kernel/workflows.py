from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import Agent, PromptedOutput

from .models import ToolRisk

WORKFLOW_SCHEMA = "amk.workflow/v1"
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TOOL_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_REFERENCE_PATTERN = re.compile(r"\$\{([^}]+)\}")
_RUNTIME_VARIABLES = {"local_date", "now", "timezone", "scheduled_for"}


class WorkflowError(ValueError):
    """Base error for workflow proposal and validation failures."""


class WorkflowGenerationError(WorkflowError):
    """The proposal model could not produce a usable structured workflow."""


class WorkflowValidationError(WorkflowError):
    """A workflow violates the AMK workflow contract."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(dict.fromkeys(str(error) for error in errors))
        super().__init__("Workflow invalide : " + "; ".join(self.errors))


class StaleWorkflowError(WorkflowValidationError):
    """The accepted workflow was proposed from a different routine basis."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkflowTool(_StrictModel):
    """One tool contract available to the routine's selected agent."""

    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    description: str = ""
    category: str = "general"
    risk_tags: list[ToolRisk] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)
    cancellable: bool = False
    persistent: bool = False
    module: str | None = None
    # Guardian metadata is part of the effective tool contract. Keeping it in
    # the workflow basis both accepts the catalog emitted by Kernel and makes
    # security-relevant catalog changes invalidate an older proposal cleanly.
    path_parameters: list[str] = Field(default_factory=list)
    url_parameters: list[str] = Field(default_factory=list)


class WorkflowBasis(_StrictModel):
    """Canonical routine configuration and effective authoring context."""

    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1)
    schedule: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="Europe/Paris", min_length=1)
    # Empty is the canonical logical representation of `workspace: null`.
    # Runtime execution resolves it to the agent's personal workspace.
    workspace: str = ""
    agent_id: str = Field(default="main", pattern=r"^[a-z0-9][a-z0-9._-]*$")
    skills: list[str] = Field(default_factory=list)
    skill_instructions: dict[str, str] = Field(default_factory=dict)
    security_mode: Literal["safe", "limited", "power"] = "limited"
    tool_catalog: list[WorkflowTool] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_context(self) -> WorkflowBasis:
        if len(self.skills) != len(set(self.skills)):
            raise ValueError("skills must be unique")
        undeclared = set(self.skill_instructions) - set(self.skills)
        if undeclared:
            raise ValueError(
                "skill_instructions contains unselected skills: " + ", ".join(sorted(undeclared))
            )
        tool_names = [tool.name for tool in self.tool_catalog]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool_catalog names must be unique")
        return self


class WorkflowExecution(_StrictModel):
    mode: Literal["agent_guided"] = "agent_guided"
    deviation: Literal["stop_and_report", "allow_declared_fallbacks"] = "stop_and_report"
    timezone: str = Field(min_length=1)


class WorkflowParameter(_StrictModel):
    type: Literal["string", "integer", "number", "boolean", "array", "object"]
    default: Any = None

    @model_validator(mode="after")
    def validate_default(self) -> WorkflowParameter:
        # `default: null` signifie « pas de valeur par defaut », ce qui est deja
        # l'etat du champ absent : refuser l'ecriture explicite creait une
        # distinction sans portee, et rejetait des propositions correctes.
        if (
            "default" in self.model_fields_set
            and self.default is not None
            and not _matches_parameter_type(self.default, self.type)
        ):
            raise ValueError(
                f"default doit être de type {self.type}, reçu "
                f"{type(self.default).__name__} ({self.default!r})"
            )
        return self


class WorkflowVariable(_StrictModel):
    source: str = Field(alias="from", min_length=1)


class WorkflowPermission(_StrictModel):
    tool: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    fixed_args: dict[str, Any] = Field(default_factory=dict)


class WorkflowPermissions(_StrictModel):
    authority: Literal["kernel_guardian"] = "kernel_guardian"
    unlisted: Literal["stop_and_report"] = "stop_and_report"
    declarations: list[WorkflowPermission] = Field(default_factory=list)


class MissingDependency(_StrictModel):
    capability: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RetryPolicy(_StrictModel):
    attempts: int = Field(default=1, ge=1, le=3)
    on: list[Literal["retryable", "timeout", "transport"]] = Field(default_factory=list)


class FallbackPolicy(_StrictModel):
    action: Literal["stop", "continue", "fail_partial"] = "stop"


class ToolEvidence(_StrictModel):
    required: Literal[True] = True
    capture: list[str] = Field(default_factory=lambda: ["data"])


class SynthesisEvidence(_StrictModel):
    from_steps: list[str] = Field(default_factory=list)


class ToolWorkflowStep(_StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: Literal["tool"] = "tool"
    needs: list[str] = Field(default_factory=list)
    tool: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    args: dict[str, Any]
    save_as: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback: FallbackPolicy = Field(default_factory=FallbackPolicy)
    evidence: ToolEvidence = Field(default_factory=ToolEvidence)


class SynthesisWorkflowStep(_StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: Literal["synthesize"] = "synthesize"
    needs: list[str] = Field(default_factory=list)
    instructions: str = Field(min_length=1)
    evidence: SynthesisEvidence = Field(default_factory=SynthesisEvidence)


WorkflowStep = Annotated[
    ToolWorkflowStep | SynthesisWorkflowStep,
    Field(discriminator="kind"),
]


class WorkflowOutput(_StrictModel):
    format: Literal["markdown", "text", "json"] = "markdown"
    language: str = Field(default="fr", min_length=1)
    sections: list[str] = Field(min_length=1)
    evidence_policy: Literal["cite_or_mark_uncertain", "captured_only"] = "cite_or_mark_uncertain"
    on_incomplete: Literal["fail", "partial_with_warnings"] = "partial_with_warnings"


class WorkflowDefinition(_StrictModel):
    schema_version: Literal["amk.workflow/v1"] = Field(
        default=WORKFLOW_SCHEMA,
        alias="schema",
    )
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    status: Literal["draft", "ready", "blocked"]
    execution: WorkflowExecution
    parameters: dict[str, WorkflowParameter] = Field(default_factory=dict)
    variables: dict[str, WorkflowVariable] = Field(default_factory=dict)
    permissions: WorkflowPermissions = Field(default_factory=WorkflowPermissions)
    missing_dependencies: list[MissingDependency] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(min_length=1)
    output: WorkflowOutput


class AcceptedWorkflow(_StrictModel):
    workflow: WorkflowDefinition
    basis_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    warnings: list[str] = Field(default_factory=list)


def workflow_basis_hash(basis: WorkflowBasis) -> str:
    """Hash a normalized basis so a proposal cannot be applied after an edit."""

    payload = basis.model_dump(mode="json", by_alias=True, exclude_none=False)
    payload["skills"] = sorted(payload["skills"])
    payload["tool_catalog"] = sorted(payload["tool_catalog"], key=lambda item: item["name"])
    for tool in payload["tool_catalog"]:
        tool["risk_tags"] = sorted(tool["risk_tags"])
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_accepted(
    accepted: AcceptedWorkflow,
    basis: WorkflowBasis,
) -> AcceptedWorkflow:
    """Revalidate an accepted proposal against its exact current routine basis."""

    current_hash = workflow_basis_hash(basis)
    if accepted.basis_hash != current_hash:
        raise StaleWorkflowError(
            ["la routine a changé depuis la génération du workflow; régénérer la proposition"]
        )
    errors = _validation_errors(accepted.workflow, basis)
    if errors:
        raise WorkflowValidationError(errors)
    return accepted


def workflow_tool_allowlist(workflow: WorkflowDefinition) -> list[str]:
    """Return the stable, exact tool list for a guided workflow."""

    return sorted({step.tool for step in workflow.steps if isinstance(step, ToolWorkflowStep)})


def render_workflow_instructions(workflow: WorkflowDefinition) -> str:
    """Render the validated contract inline; executing it requires no read call."""

    document = workflow.model_dump(mode="json", by_alias=True, exclude_none=True)
    serialized = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "# Workflow de routine accepté\n\n"
        "Le contrat ci-dessous est déjà chargé dans tes instructions. Ne tente pas de "
        "le relire depuis un fichier.\n\n"
        "- Refuse de l’exécuter si son statut n’est pas `ready`.\n"
        "- Résous les paramètres et variables depuis le contexte d’exécution courant.\n"
        "- Respecte le graphe `needs`, les outils et les arguments déclarés.\n"
        "- N’utilise aucun outil absent des étapes et n’improvise aucun fallback.\n"
        "- Le Guardian reste l’unique autorité pour permettre ou refuser un appel réel.\n"
        "- Ne fabrique aucune donnée manquante et applique la politique de preuve.\n\n"
        "```json\n"
        f"{serialized}\n"
        "```"
    )


class WorkflowProposalService:
    """Generate validated workflow previews without tools, sessions, events, or writes."""

    def __init__(
        self,
        model: Any,
        *,
        workflow_creator: str,
        retries: int = 1,
    ) -> None:
        if not workflow_creator.strip():
            raise ValueError("workflow_creator instructions cannot be empty")
        if not 0 <= retries <= 3:
            raise ValueError("retries must be between 0 and 3")
        self.model = model
        self.workflow_creator = workflow_creator.strip()
        self.retries = retries

    async def propose(self, basis: WorkflowBasis) -> AcceptedWorkflow:
        instructions = _proposal_instructions(self.workflow_creator)
        agent = Agent(
            self.model,
            # A bare Pydantic model defaults to Pydantic AI's tool-output mode,
            # which forces an artificial output tool through ``tool_choice``.
            # Several reasoning APIs (notably DeepSeek V4 thinking mode) reject
            # that parameter even though they support JSON responses. Prompted
            # output keeps schema injection, parsing, validation and repair
            # retries while receiving the document as normal response text.
            output_type=PromptedOutput(WorkflowDefinition),
            instructions=instructions,
            retries=1,
        )
        request = _proposal_request(basis)
        previous_errors: list[str] = []
        for attempt in range(self.retries + 1):
            prompt = request
            if previous_errors:
                prompt += (
                    "\n\nLa proposition précédente a été refusée par le validateur. "
                    "Corrige uniquement ces erreurs :\n- " + "\n- ".join(previous_errors)
                )
            try:
                result = await agent.run(prompt)
            except Exception as exc:
                raise WorkflowGenerationError(
                    f"Impossible de générer la proposition structurée : {exc}"
                ) from exc
            accepted = AcceptedWorkflow(
                workflow=result.output,
                basis_hash=workflow_basis_hash(basis),
                warnings=_workflow_warnings(result.output),
            )
            try:
                return validate_accepted(accepted, basis)
            except WorkflowValidationError as exc:
                previous_errors = list(exc.errors)
                if attempt >= self.retries:
                    raise
        raise AssertionError("unreachable")


def _proposal_instructions(workflow_creator: str) -> str:
    return "\n\n".join(
        [
            """Tu utilises workflow-creator uniquement en mode proposition.
Tu ne disposes d’aucun outil et tu ne dois exécuter, écrire, enregistrer, connecter,
autoriser ou modifier quoi que ce soit. Retourne seulement le WorkflowDefinition
structuré demandé. Le serveur décidera ensuite s’il peut être présenté à l’utilisateur.""",
            workflow_creator,
            """Contraintes prioritaires du mode proposition :
- Utilise exclusivement les outils du catalogue effectif fourni avec la demande.
- Ne transforme jamais une capacité absente en faux nom d’outil.
- Un exemple du manuel workflow-creator n’est jamais une exigence de la routine.
- Ignore notamment tout exemple de calendrier, agenda, CalDAV ou autre connecteur
  s’il n’est pas explicitement requis par la demande de routine ou une skill sélectionnée.
- Les dépendances manquantes doivent provenir de la demande réelle ou des skills
  sélectionnées, jamais des exemples de ce manuel.
- Une routine peut légitimement ne comporter qu’une synthèse et aucun appel d’outil.
- Conserve kernel_guardian comme autorité; ne présume jamais qu’une permission est acquise.
- Le statut ready exige zéro dépendance manquante; blocked exige de les documenter.
- N’inclus aucun secret et n’utilise aucune date figée au moment de la proposition.""",
            # Ces règles sont appliquées par validate_accepted(). Le manuel
            # workflow-creator les renvoie vers references/workflow-format.md,
            # que le modèle ne peut pas lire en mode proposition puisqu'il n'a
            # aucun outil. Non énoncées ici, elles étaient devinées — d'où des
            # propositions systématiquement rejetées sur la syntaxe.
            """Syntaxe vérifiée par le validateur. Toute déviation fait rejeter la proposition :
- Référence différée : `${racine.chemin}`, avec UNE seule paire d’accolades précédée
  d’un `$`. N’écris jamais `${{...}}`, `{{...}}` ni `{...}`.
- Seules trois racines existent : `${parameters.nom}`, `${variables.nom}`,
  `${steps.identifiant.champ}`. Toute autre racine est refusée.
- La racine n’est jamais implicite. `${start_date}` est refusé même si
  `start_date` est déclaré dans `variables` : écris `${variables.start_date}`.
  Toute référence contient donc au moins un point.
- Un `${parameters.nom}` doit correspondre à une clé déclarée dans `parameters`,
  un `${variables.nom}` à une clé déclarée dans `variables`, et un
  `${steps.identifiant...}` à un `steps[].id` existant qui figure dans le
  `needs` de l’étape courante.
- Chaque entrée de `variables` a un champ `from` commençant obligatoirement par
  `runtime.` : `runtime.local_date`, `runtime.now`, `runtime.timezone` ou
  `runtime.scheduled_for`.
- `steps[].id` est en minuscules, chiffres et tirets (`^[a-z0-9][a-z0-9-]*$`).
  Les noms de `parameters` et `variables` suivent la même forme, tiret ou
  underscore au choix.
- `steps[].evidence.from_steps` ne contient que des `steps[].id` déclarés, et
  uniquement ceux dont l’étape courante dépend via `needs` — directement ou
  transitivement. N’invente jamais un nom de résultat comme `events_data` ou
  `news_results_1` : utilise l’identifiant de l’étape elle-même.""",
        ]
    )


def _proposal_request(basis: WorkflowBasis) -> str:
    skills = [
        {
            "name": name,
            "instructions": basis.skill_instructions.get(name, ""),
        }
        for name in basis.skills
    ]
    tools = [
        tool.model_dump(mode="json", exclude_none=True)
        for tool in sorted(basis.tool_catalog, key=lambda item: item.name)
    ]
    routine = {
        "name": basis.name,
        "prompt": basis.prompt,
        "schedule": basis.schedule,
        "timezone": basis.timezone,
        "workspace": basis.workspace,
        "agent_id": basis.agent_id,
        "security_mode": basis.security_mode,
    }
    return (
        "Propose le workflow minimal qui satisfait cette routine. Les seuls besoins "
        "fonctionnels autorisés sont ceux de `routine.prompt` et des `selected_skills`. "
        "Les descriptions d’outils indiquent des capacités disponibles, pas des étapes "
        "obligatoires.\n\n"
        + json.dumps(
            {
                "routine": routine,
                "selected_skills": skills,
                "effective_tool_catalog": tools,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _workflow_warnings(workflow: WorkflowDefinition) -> list[str]:
    warnings = [
        f"Dépendance manquante : {item.capability} — {item.reason}"
        for item in workflow.missing_dependencies
    ]
    if workflow.status != "ready":
        warnings.append(f"Workflow non exécutable tant que son statut est {workflow.status}.")
    return warnings


def _validation_errors(
    workflow: WorkflowDefinition,
    basis: WorkflowBasis,
) -> list[str]:
    errors: list[str] = []
    if workflow.execution.timezone != basis.timezone:
        errors.append("execution.timezone doit correspondre au fuseau de la routine")
    if workflow.status == "ready" and workflow.missing_dependencies:
        errors.append("un workflow ready ne peut pas avoir de dépendance manquante")
    if workflow.status == "blocked" and not workflow.missing_dependencies:
        errors.append("un workflow blocked doit documenter une dépendance manquante")

    parameter_names = set(workflow.parameters)
    variable_names = set(workflow.variables)
    for name in [*parameter_names, *variable_names]:
        if not _ID_PATTERN.fullmatch(name.replace("_", "-")):
            errors.append(f"nom de paramètre ou variable invalide: {name}")
    for name, variable in workflow.variables.items():
        if not variable.source.startswith("runtime."):
            errors.append(
                f"variables.{name}.from doit commencer par 'runtime.' "
                "(runtime.local_date, runtime.now, runtime.timezone ou "
                f"runtime.scheduled_for), reçu : {variable.source!r}"
            )
            continue
        runtime_name = variable.source.split(".", 1)[1]
        if runtime_name not in _RUNTIME_VARIABLES:
            errors.append(f"variables.{name}.from référence un champ runtime inconnu")

    step_ids = [step.id for step in workflow.steps]
    if len(step_ids) != len(set(step_ids)):
        errors.append("les ids d’étape doivent être uniques")
    known_steps = set(step_ids)
    graph: dict[str, list[str]] = {}
    for step in workflow.steps:
        if len(step.needs) != len(set(step.needs)):
            errors.append(f"steps.{step.id}.needs contient un doublon")
        unknown = set(step.needs) - known_steps
        if unknown:
            errors.append(f"steps.{step.id}.needs contient des ids inconnus: {sorted(unknown)}")
        if step.id in step.needs:
            errors.append(f"steps.{step.id} dépend de lui-même")
        graph[step.id] = list(step.needs)
    if _has_cycle(graph):
        errors.append("le graphe des étapes contient un cycle")
    ancestors = {step_id: _dependency_ancestors(step_id, graph) for step_id in known_steps}

    catalog = {tool.name: tool for tool in basis.tool_catalog}
    tool_steps = [step for step in workflow.steps if isinstance(step, ToolWorkflowStep)]
    for step in tool_steps:
        descriptor = catalog.get(step.tool)
        if descriptor is None:
            errors.append(f"steps.{step.id}.tool inconnu dans le catalogue: {step.tool}")
            continue
        errors.extend(_argument_errors(step, descriptor.input_schema))

    declarations: dict[str, WorkflowPermission] = {}
    for declaration in workflow.permissions.declarations:
        if declaration.tool in declarations:
            errors.append(f"permission déclarée plusieurs fois: {declaration.tool}")
        declarations[declaration.tool] = declaration
        if declaration.tool not in catalog:
            errors.append(f"permission pour un outil inconnu: {declaration.tool}")
    used_tools = {step.tool for step in tool_steps}
    for tool in sorted(used_tools - declarations.keys()):
        errors.append(f"outil non déclaré dans permissions: {tool}")
    for tool in sorted(declarations.keys() - used_tools):
        errors.append(f"permission inutilisée: {tool}")
    for step in tool_steps:
        declaration = declarations.get(step.tool)
        if declaration is None:
            continue
        for key, value in declaration.fixed_args.items():
            if step.args.get(key) != value:
                errors.append(f"steps.{step.id} ne respecte pas fixed_args.{key}")

    for step in workflow.steps:
        errors.extend(
            _reference_errors(
                step.model_dump(mode="json", by_alias=True),
                location=f"steps.{step.id}",
                parameters=parameter_names,
                variables=variable_names,
                steps=known_steps,
                available_steps=ancestors[step.id],
            )
        )
        if isinstance(step, SynthesisWorkflowStep):
            unknown_evidence = set(step.evidence.from_steps) - known_steps
            if unknown_evidence:
                errors.append(
                    f"steps.{step.id}.evidence.from_steps contient des ids inconnus: "
                    f"{sorted(unknown_evidence)}"
                )
            unavailable_evidence = (
                set(step.evidence.from_steps) - ancestors[step.id] - unknown_evidence
            )
            if unavailable_evidence:
                errors.append(
                    f"steps.{step.id}.evidence.from_steps doit provenir de needs: "
                    f"{sorted(unavailable_evidence)}"
                )

    errors.extend(
        _reference_errors(
            workflow.output.model_dump(mode="json"),
            location="output",
            parameters=parameter_names,
            variables=variable_names,
            steps=known_steps,
        )
    )
    return list(dict.fromkeys(errors))


def _argument_errors(step: ToolWorkflowStep, schema: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    properties = schema.get("properties", {})
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required", [])
    required = set(required) if isinstance(required, list) else set()
    missing = required - step.args.keys()
    if missing:
        errors.append(f"steps.{step.id}.args requis absents: {sorted(missing)}")
    if schema.get("additionalProperties") is False:
        unknown = step.args.keys() - properties.keys()
        if unknown:
            errors.append(f"steps.{step.id}.args inconnus: {sorted(unknown)}")
    accepts_justification = (
        "justification" in properties or schema.get("additionalProperties") is not False
    )
    justification = step.args.get("justification")
    if accepts_justification and (not isinstance(justification, str) or not justification.strip()):
        errors.append(f"steps.{step.id}.args.justification doit être une chaîne non vide")
    for name, value in step.args.items():
        definition = properties.get(name)
        if isinstance(definition, Mapping) and not _matches_json_schema(value, definition):
            errors.append(f"steps.{step.id}.args.{name} ne respecte pas le schéma de l’outil")
    return errors


def _matches_json_schema(value: Any, schema: Mapping[str, Any]) -> bool:
    if isinstance(value, str) and _REFERENCE_PATTERN.search(value):
        return True
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        return any(
            isinstance(item, Mapping) and _matches_json_schema(value, item) for item in variants
        )
    if "enum" in schema and value not in schema["enum"]:
        return False
    expected = schema.get("type")
    matches = {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
        "null": lambda item: item is None,
    }
    if isinstance(expected, list):
        return any(_matches_json_schema(value, {**schema, "type": item}) for item in expected)
    if isinstance(expected, str) and expected in matches and not matches[expected](value):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
    return True


def _reference_errors(
    value: Any,
    *,
    location: str,
    parameters: set[str],
    variables: set[str],
    steps: set[str],
    available_steps: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for text in _walk_strings(value):
        for reference in _REFERENCE_PATTERN.findall(text):
            root, separator, tail = reference.partition(".")
            if not separator or not tail:
                # Reference sans racine (`${start_date}`). Si le nom existe
                # ailleurs dans le document, on donne directement la forme
                # attendue plutot qu'un simple constat d'invalidite.
                bare = reference.strip()
                suggestion = next(
                    (
                        f"{namespace}.{bare}"
                        for namespace, names in (
                            ("variables", variables),
                            ("parameters", parameters),
                            ("steps", steps),
                        )
                        if bare in names
                    ),
                    "",
                )
                errors.append(
                    f"{location} référence {reference!r} sans racine : "
                    + (
                        f"écrire ${{{suggestion}}}"
                        if suggestion
                        else "toute référence doit commencer par parameters., variables. ou steps."
                    )
                )
            elif root == "parameters" and tail not in parameters:
                errors.append(f"{location} référence un paramètre inconnu: {reference}")
            elif root == "variables" and tail not in variables:
                errors.append(f"{location} référence une variable inconnue: {reference}")
            elif root == "steps" and tail.split(".", 1)[0] not in steps:
                errors.append(f"{location} référence une étape inconnue: {reference}")
            elif (
                root == "steps"
                and available_steps is not None
                and tail.split(".", 1)[0] not in available_steps
            ):
                errors.append(
                    f"{location} référence une étape absente de son graphe needs: {reference}"
                )
            elif root not in {"parameters", "variables", "steps"}:
                # Une racine commencant par "{" trahit une double accolade
                # (`${{...}}`). Le message brut affichait alors un fragment
                # tronque, illisible pour l'utilisateur comme pour la boucle
                # de reparation : on nomme la cause.
                if root.startswith("{"):
                    errors.append(
                        f"{location} utilise ${{{{...}}}} au lieu de ${{...}} : "
                        f"écrire ${{{root.lstrip('{')}.{tail.rstrip('}')}}}"
                    )
                else:
                    errors.append(
                        f"{location} contient une référence non supportée: {reference} "
                        "(racines autorisées : parameters, variables, steps)"
                    )
    return errors


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _has_cycle(graph: Mapping[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited or node not in graph:
            return False
        visiting.add(node)
        if any(visit(dependency) for dependency in graph.get(node, [])):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)


def _dependency_ancestors(node: str, graph: Mapping[str, list[str]]) -> set[str]:
    ancestors: set[str] = set()
    pending = list(graph.get(node, []))
    while pending:
        dependency = pending.pop()
        if dependency in ancestors:
            continue
        ancestors.add(dependency)
        pending.extend(graph.get(dependency, []))
    ancestors.discard(node)
    return ancestors


def _matches_parameter_type(value: Any, expected: str) -> bool:
    return {
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
    }[expected](value)


__all__ = [
    "AcceptedWorkflow",
    "FallbackPolicy",
    "MissingDependency",
    "RetryPolicy",
    "StaleWorkflowError",
    "SynthesisWorkflowStep",
    "ToolEvidence",
    "ToolWorkflowStep",
    "WorkflowBasis",
    "WorkflowDefinition",
    "WorkflowError",
    "WorkflowExecution",
    "WorkflowGenerationError",
    "WorkflowOutput",
    "WorkflowParameter",
    "WorkflowPermission",
    "WorkflowPermissions",
    "WorkflowProposalService",
    "WorkflowTool",
    "WorkflowValidationError",
    "WorkflowVariable",
    "render_workflow_instructions",
    "validate_accepted",
    "workflow_basis_hash",
    "workflow_tool_allowlist",
]
