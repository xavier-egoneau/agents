from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from agentic_kernel.workflows import (
    AcceptedWorkflow,
    StaleWorkflowError,
    WorkflowBasis,
    WorkflowDefinition,
    WorkflowProposalService,
    WorkflowTool,
    WorkflowValidationError,
    render_workflow_instructions,
    validate_accepted,
    workflow_basis_hash,
    workflow_tool_allowlist,
)


def web_tool() -> WorkflowTool:
    return WorkflowTool(
        name="web_search",
        description="Search public web pages.",
        category="web",
        risk_tags=["network"],
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "justification": {"type": "string"},
            },
            "required": ["query", "justification"],
            "additionalProperties": False,
        },
    )


def basis(tmp_path: Path, **updates) -> WorkflowBasis:
    values = {
        "name": "Veille du matin",
        "prompt": "Recherche les actualités importantes et résume-les avec leurs sources.",
        "schedule": "0 9 * * *",
        "timezone": "Europe/Paris",
        "workspace": str(tmp_path),
        "agent_id": "main",
        "skills": ["editorial-style"],
        "skill_instructions": {
            "editorial-style": "Rédige une synthèse française concise et cite les sources."
        },
        "security_mode": "limited",
        "tool_catalog": [web_tool()],
    }
    values.update(updates)
    return WorkflowBasis.model_validate(values)


def workflow(**updates) -> WorkflowDefinition:
    values = {
        "schema": "amk.workflow/v1",
        "id": "veille-du-matin-v1",
        "title": "Veille du matin",
        "status": "ready",
        "execution": {
            "mode": "agent_guided",
            "deviation": "stop_and_report",
            "timezone": "Europe/Paris",
        },
        "parameters": {"topic": {"type": "string", "default": "actualité"}},
        "variables": {"today": {"from": "runtime.local_date"}},
        "permissions": {
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": "web_search", "fixed_args": {"limit": 5}}],
        },
        "missing_dependencies": [],
        "steps": [
            {
                "id": "news",
                "kind": "tool",
                "needs": [],
                "tool": "web_search",
                "args": {
                    "query": "${parameters.topic} ${variables.today}",
                    "limit": 5,
                    "justification": "Rechercher les actualités demandées par la routine.",
                },
                "save_as": "news_results",
                "retry": {"attempts": 2, "on": ["retryable"]},
                "fallback": {"action": "fail_partial"},
                "evidence": {"required": True, "capture": ["data"]},
            },
            {
                "id": "summary",
                "kind": "synthesize",
                "needs": ["news"],
                "instructions": "Résumer les résultats capturés sans rien inventer.",
                "evidence": {"from_steps": ["news"]},
            },
        ],
        "output": {
            "format": "markdown",
            "language": "fr",
            "sections": ["À retenir", "Sources et incertitudes"],
            "evidence_policy": "cite_or_mark_uncertain",
            "on_incomplete": "partial_with_warnings",
        },
    }
    values.update(updates)
    return WorkflowDefinition.model_validate(values)


def accepted(definition: WorkflowDefinition, current_basis: WorkflowBasis) -> AcceptedWorkflow:
    return AcceptedWorkflow(
        workflow=definition,
        basis_hash=workflow_basis_hash(current_basis),
    )


def test_validate_known_tools_arguments_permissions_and_references(tmp_path: Path) -> None:
    current_basis = basis(tmp_path)
    proposal = accepted(workflow(), current_basis)

    assert validate_accepted(proposal, current_basis) is proposal
    assert workflow_tool_allowlist(proposal.workflow) == ["web_search"]


def test_justification_is_not_invented_for_a_strict_tool_that_does_not_accept_it(
    tmp_path: Path,
) -> None:
    delegate = WorkflowTool(
        name="agent_delegate",
        input_schema={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["agent_name", "task"],
            "additionalProperties": False,
        },
    )
    current_basis = basis(tmp_path, tool_catalog=[delegate])
    definition = workflow(
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": "agent_delegate"}],
        },
        steps=[
            {
                "id": "delegate",
                "kind": "tool",
                "tool": "agent_delegate",
                "args": {"agent_name": "reviewer", "task": "Relire la synthèse."},
                "evidence": {"required": True, "capture": ["data"]},
            },
            {
                "id": "summary",
                "kind": "synthesize",
                "needs": ["delegate"],
                "instructions": "Restituer la relecture.",
                "evidence": {"from_steps": ["delegate"]},
            },
        ],
    )

    validate_accepted(accepted(definition, current_basis), current_basis)


def test_unknown_tool_is_rejected(tmp_path: Path) -> None:
    current_basis = basis(tmp_path)
    definition = workflow(
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": "get_agenda", "fixed_args": {}}],
        },
        steps=[
            {
                "id": "agenda",
                "kind": "tool",
                "tool": "get_agenda",
                "args": {"justification": "Lire l’agenda."},
                "evidence": {"required": True, "capture": ["data"]},
            },
            {
                "id": "summary",
                "kind": "synthesize",
                "needs": ["agenda"],
                "instructions": "Résumer.",
                "evidence": {"from_steps": ["agenda"]},
            },
        ],
    )

    with pytest.raises(WorkflowValidationError, match="outil inconnu"):
        validate_accepted(accepted(definition, current_basis), current_basis)


def test_unknown_and_invalid_tool_arguments_are_rejected(tmp_path: Path) -> None:
    current_basis = basis(tmp_path)
    raw = workflow().model_dump(mode="json", by_alias=True)
    raw["steps"][0]["args"]["limit"] = 99
    raw["steps"][0]["args"]["invented"] = True
    definition = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("args inconnus" in error for error in caught.value.errors)
    assert any("args.limit" in error for error in caught.value.errors)


def test_explicit_null_default_is_accepted() -> None:
    """`default: null` dit « pas de valeur par défaut ».

    C'est déjà l'état du champ absent : refuser l'écriture explicite créait une
    distinction sans portée fonctionnelle, et rejetait des propositions valides.
    """
    from agentic_kernel.workflows import WorkflowParameter

    assert WorkflowParameter.model_validate({"type": "string", "default": None}).default is None
    assert WorkflowParameter.model_validate({"type": "string"}).default is None


def test_mismatched_default_reports_what_was_received() -> None:
    from pydantic import ValidationError

    from agentic_kernel.workflows import WorkflowParameter

    with pytest.raises(ValidationError, match=r"reçu int \(42\)"):
        WorkflowParameter.model_validate({"type": "string", "default": 42})


def test_double_brace_reference_names_the_syntax_error(tmp_path: Path) -> None:
    """`${{...}}` est l'erreur de syntaxe la plus fréquente des modèles.

    Le message brut affichait un fragment tronqué (`{variables.today`) que ni
    l'utilisateur ni la boucle de réparation ne pouvaient exploiter.
    """
    current_basis = basis(tmp_path)
    raw = workflow().model_dump(mode="json", by_alias=True)
    raw["steps"][0]["args"]["query"] = "${{variables.today}}"
    definition = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("au lieu de ${...}" in error for error in caught.value.errors)
    assert any("${variables.today}" in error for error in caught.value.errors)


def test_rootless_reference_suggests_the_qualified_form(tmp_path: Path) -> None:
    """`${today}` au lieu de `${variables.today}` : la racine n'est pas implicite."""
    current_basis = basis(tmp_path)
    raw = workflow().model_dump(mode="json", by_alias=True)
    raw["steps"][0]["args"]["query"] = "${today}"
    definition = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("${variables.today}" in error for error in caught.value.errors)


def test_rootless_unknown_reference_lists_the_namespaces(tmp_path: Path) -> None:
    current_basis = basis(tmp_path)
    raw = workflow().model_dump(mode="json", by_alias=True)
    raw["steps"][0]["args"]["query"] = "${inconnu}"
    definition = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("parameters., variables. ou steps." in error for error in caught.value.errors)


def test_variable_source_error_lists_the_accepted_runtime_keys(tmp_path: Path) -> None:
    current_basis = basis(tmp_path)
    raw = workflow().model_dump(mode="json", by_alias=True)
    raw["variables"]["today"] = {"from": "context.local_date"}
    definition = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("runtime.local_date" in error for error in caught.value.errors)
    assert any("context.local_date" in error for error in caught.value.errors)


def test_invented_evidence_ids_are_rejected(tmp_path: Path) -> None:
    """Le modèle référençait `save_as` au lieu de l'identifiant de l'étape."""
    current_basis = basis(tmp_path)
    raw = workflow().model_dump(mode="json", by_alias=True)
    raw["steps"][1]["evidence"]["from_steps"] = ["news_results"]
    definition = WorkflowDefinition.model_validate(raw)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("ids inconnus" in error for error in caught.value.errors)


def test_proposal_instructions_state_the_validated_syntax() -> None:
    """Les règles appliquées par le validateur doivent être énoncées au modèle.

    Elles vivaient uniquement dans references/workflow-format.md, que le modèle
    ne peut pas lire en mode proposition puisqu'il n'a aucun outil.
    """
    from agentic_kernel.workflows import _proposal_instructions

    instructions = _proposal_instructions("Manuel workflow-creator.")

    assert "${variables.nom}" in instructions
    assert "${{" in instructions
    assert "runtime.local_date" in instructions
    assert "from_steps" in instructions


def test_cycle_and_unknown_reference_are_rejected(tmp_path: Path) -> None:
    current_basis = basis(tmp_path)
    definition = workflow(
        parameters={},
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [],
        },
        steps=[
            {
                "id": "first",
                "kind": "synthesize",
                "needs": ["second"],
                "instructions": "Utilise ${parameters.absent}.",
                "evidence": {"from_steps": ["second"]},
            },
            {
                "id": "second",
                "kind": "synthesize",
                "needs": ["first"],
                "instructions": "Termine la synthèse.",
                "evidence": {"from_steps": ["first"]},
            },
        ],
    )

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("cycle" in error for error in caught.value.errors)
    assert any("paramètre inconnu" in error for error in caught.value.errors)


def test_step_outputs_must_be_declared_in_the_needs_graph(tmp_path: Path) -> None:
    current_basis = basis(tmp_path, tool_catalog=[])
    definition = workflow(
        parameters={},
        variables={},
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [],
        },
        steps=[
            {
                "id": "source",
                "kind": "synthesize",
                "instructions": "Produire une matière intermédiaire.",
                "evidence": {"from_steps": []},
            },
            {
                "id": "summary",
                "kind": "synthesize",
                "needs": [],
                "instructions": "Utiliser ${steps.source.output}.",
                "evidence": {"from_steps": ["source"]},
            },
        ],
    )

    with pytest.raises(WorkflowValidationError) as caught:
        validate_accepted(accepted(definition, current_basis), current_basis)

    assert any("graphe needs" in error for error in caught.value.errors)
    assert any("doit provenir de needs" in error for error in caught.value.errors)


def test_stale_basis_hash_is_rejected(tmp_path: Path) -> None:
    original = basis(tmp_path)
    proposal = accepted(workflow(), original)
    changed = original.model_copy(update={"prompt": "Une autre demande"})

    with pytest.raises(StaleWorkflowError, match="routine a changé"):
        validate_accepted(proposal, changed)


def test_basis_hash_is_canonical_for_catalog_and_skill_order(tmp_path: Path) -> None:
    clock = WorkflowTool(name="utc_now", input_schema={"type": "object"})
    first = basis(
        tmp_path,
        skills=["a", "b"],
        skill_instructions={"a": "A", "b": "B"},
        tool_catalog=[web_tool(), clock],
    )
    second = basis(
        tmp_path,
        skills=["b", "a"],
        skill_instructions={"b": "B", "a": "A"},
        tool_catalog=[clock, web_tool()],
    )

    assert workflow_basis_hash(first) == workflow_basis_hash(second)


def test_synthesis_only_has_empty_allowlist_and_inline_instructions(tmp_path: Path) -> None:
    definition = workflow(
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [],
        },
        steps=[
            {
                "id": "summary",
                "kind": "synthesize",
                "instructions": "Applique uniquement la skill éditoriale sélectionnée.",
                "evidence": {"from_steps": []},
            }
        ],
    )
    current_basis = basis(tmp_path, tool_catalog=[])

    validate_accepted(accepted(definition, current_basis), current_basis)
    assert workflow_tool_allowlist(definition) == []
    rendered = render_workflow_instructions(definition)
    assert '"kind": "synthesize"' in rendered
    assert "Ne tente pas de le relire depuis un fichier" in rendered
    assert "workflow.yaml" not in rendered


async def test_proposal_service_is_structured_toolless_and_has_no_side_effects(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    definition = workflow(
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [],
        },
        steps=[
            {
                "id": "summary",
                "kind": "synthesize",
                "instructions": "Résumer uniquement la matière disponible.",
                "evidence": {"from_steps": []},
            }
        ],
    )

    def respond(messages, info):
        observed["function_tools"] = [tool.name for tool in info.function_tools]
        observed["output_tools"] = [tool.name for tool in info.output_tools]
        observed["output_mode"] = info.model_request_parameters.output_mode
        observed["instructions"] = info.instructions or ""
        observed["messages"] = messages
        return ModelResponse(
            parts=[
                TextPart(
                    definition.model_dump_json(by_alias=True),
                )
            ]
        )

    current_basis = basis(
        tmp_path,
        prompt="Résume les notes importantes.",
        tool_catalog=[],
        skills=[],
        skill_instructions={},
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    service = WorkflowProposalService(
        FunctionModel(respond),
        workflow_creator="Exemple illustratif : connecter un agenda CalDAV.",
        retries=0,
    )

    proposal = await service.propose(current_basis)

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert proposal.workflow == definition
    assert proposal.basis_hash == workflow_basis_hash(current_basis)
    assert observed["function_tools"] == []
    assert observed["output_tools"] == []
    assert observed["output_mode"] == "prompted"
    assert "Ignore notamment tout exemple de calendrier" in str(observed["instructions"])
    assert "Always respond with a JSON object" in str(observed["instructions"])
    assert "Les seuls besoins fonctionnels autorisés" in str(observed["messages"])
    assert proposal.workflow.missing_dependencies == []
    assert before == after == []


async def test_prompted_workflow_output_omits_tools_and_tool_choice_for_deepseek(
    tmp_path: Path,
) -> None:
    definition = workflow()
    observed_request: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        observed_request.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "workflow-proposal",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": definition.model_dump_json(by_alias=True),
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        client = AsyncOpenAI(
            api_key="test-key",
            base_url="https://api.deepseek.com",
            http_client=http_client,
        )
        model = OpenAIChatModel(
            "deepseek-v4-pro",
            provider=OpenAIProvider(openai_client=client),
        )
        proposal = await WorkflowProposalService(
            model,
            workflow_creator="Définis une procédure minimale.",
            retries=0,
        ).propose(basis(tmp_path))

    assert proposal.workflow == definition
    assert "tools" not in observed_request
    assert "tool_choice" not in observed_request
    assert observed_request["response_format"] == {"type": "json_object"}


async def test_proposal_service_repairs_one_catalog_validation_error(tmp_path: Path) -> None:
    calls = 0
    invalid = workflow(
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": "invented_tool", "fixed_args": {}}],
        },
        steps=[
            {
                "id": "invented",
                "kind": "tool",
                "tool": "invented_tool",
                "args": {"justification": "Essayer un outil imaginaire."},
                "evidence": {"required": True, "capture": ["data"]},
            }
        ],
    )
    valid = workflow()

    def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert "outil inconnu" in str(messages)
        selected = invalid if calls == 1 else valid
        return ModelResponse(
            parts=[
                TextPart(
                    selected.model_dump_json(by_alias=True),
                )
            ]
        )

    proposal = await WorkflowProposalService(
        FunctionModel(respond),
        workflow_creator="Définis une procédure minimale.",
        retries=1,
    ).propose(basis(tmp_path))

    assert calls == 2
    assert proposal.workflow == valid


async def test_proposal_service_surfaces_final_validation_errors(tmp_path: Path) -> None:
    invalid = workflow(
        permissions={
            "authority": "kernel_guardian",
            "unlisted": "stop_and_report",
            "declarations": [{"tool": "invented_tool", "fixed_args": {}}],
        },
        steps=[
            {
                "id": "invented",
                "kind": "tool",
                "tool": "invented_tool",
                "args": {"justification": "Essayer un outil imaginaire."},
                "evidence": {"required": True, "capture": ["data"]},
            }
        ],
    )

    def respond(_messages, info):
        return ModelResponse(
            parts=[
                TextPart(
                    invalid.model_dump_json(by_alias=True),
                )
            ]
        )

    service = WorkflowProposalService(
        FunctionModel(respond),
        workflow_creator="Définis une procédure minimale.",
        retries=0,
    )

    with pytest.raises(WorkflowValidationError, match="invented_tool"):
        await service.propose(basis(tmp_path))
