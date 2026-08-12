from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.toolsets import FilteredToolset

from .compaction import ContextWindowCompaction
from .context_service import effective_context_window
from .errors import ConfigurationError
from .guardian import GuardianToolset
from .models import SecurityMode
from .orchestration import RuntimeDeps, make_neutral_subagent_toolset, make_subagents
from .providers import compaction_trigger_ratio, server_context_cap
from .skills import (
    inlined_skills,
    render_skill,
    skill_catalog_instruction,
    skill_toolset,
)
from .workflows import WorkflowDefinition, render_workflow_instructions


class AgentFactory:
    """Compose configured and ephemeral agents behind one internal boundary."""

    def __init__(
        self,
        *,
        config,
        module_registry,
        context,
        context_registry,
        workspace_maps,
        runtime_instruction: Callable[..., str],
    ) -> None:
        self.config = config
        self.module_registry = module_registry
        self.context = context
        self.context_registry = context_registry
        self.workspace_maps = workspace_maps
        self.runtime_instruction = runtime_instruction

    def build(  # noqa: C901 - dette: assemblage d'agent multi-branches
        self,
        agent_id: str,
        configs,
        provider_factory,
        budgets,
        depth: int,
        skills,
        runtime_skills: list[str] | None = None,
        workflow: dict[str, Any] | None = None,
        tool_allowlist: list[str] | None = None,
        knowledge_instruction: str = "",
        workspace: Path | None = None,
        security_mode: SecurityMode | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        force_compaction: bool = False,
    ) -> Agent[RuntimeDeps, Any]:
        if depth > budgets.max_depth:
            raise ConfigurationError(
                f"agent graph exceeds configured depth {budgets.max_depth} at {agent_id}"
            )
        config = configs[agent_id]
        resolved_provider_id = provider_override or config.provider
        provider_config = provider_factory.get_config(resolved_provider_id)
        resolved_model_name = (
            model_override
            or config.model
            or provider_config.model
        )
        context_window_tokens = effective_context_window(
            self.context_registry.get(resolved_provider_id, resolved_model_name),
            server_context_cap(provider_config),
        )
        trigger_ratio = compaction_trigger_ratio(provider_config)
        loaded_modules = self.module_registry.load_enabled()
        implicit_skills = ["user-memory"] if config.user_memory and "user-memory" in skills else []
        requested_skills = list(
            dict.fromkeys([*config.skills, *implicit_skills, *(runtime_skills or [])])
        )
        runtime_allowlist = None if tool_allowlist is None else set(tool_allowlist)
        missing_skills = set(requested_skills) - skills.keys()
        if missing_skills:
            raise ConfigurationError(
                f"agent {agent_id} references unknown skills: {sorted(missing_skills)}"
            )
        # Le modèle choisi pour le run descend dans toute la chaîne de
        # délégation. Le champ `model` d'un agent n'est qu'un défaut : basculer
        # d'un modèle à l'autre au milieu d'un run coûte un rechargement complet
        # — prohibitif en local — pour un choix que l'utilisateur vient
        # justement de faire à l'échelle du run.
        child_agents = [
            self.build(
                child,
                configs,
                provider_factory,
                budgets,
                depth + 1,
                skills,
                workflow=workflow,
                tool_allowlist=tool_allowlist,
                workspace=workspace,
                security_mode=security_mode,
                provider_override=provider_override,
                model_override=model_override,
                knowledge_instruction=knowledge_instruction,
            )
            for child in config.delegates
        ]
        active_workspace = workspace or self.config.agent_workspace(agent_id)
        active_security = security_mode or SecurityMode.LIMITED
        user_memory_instruction = (
            self.config.user_memory_instruction(agent_id) if config.user_memory else ""
        )
        instructions = [
            self.config.system_instructions(),
            self.runtime_instruction(
                active_workspace,
                active_security,
                provider_id=resolved_provider_id,
                model_name=resolved_model_name,
                context_window_tokens=context_window_tokens,
                compaction_threshold_ratio=trigger_ratio,
                agent_id=config.id,
                agent_description=config.description,
            ),
            self.workspace_maps.build(active_workspace).render(),
            self.context.secret_catalog_instruction(),
            config.instructions,
        ]
        if user_memory_instruction:
            instructions.append(user_memory_instruction)
        # Placée après la mémoire personnelle et avant les skills : c'est du
        # matériel de référence, pas une consigne de comportement.
        if knowledge_instruction:
            instructions.append(knowledge_instruction)
        # Les skills rattachées à l'agent n'entrent plus en entier dans le
        # prompt : seules celles qui se déclarent `load: always` et celles
        # demandées pour ce run précis y figurent. Les autres sont annoncées par
        # l'index plus bas et arrivent par `load_skills` au moment utile.
        #
        # Sauf si ce run n'a justement pas le droit d'appeler `load_skills` :
        # l'index désignerait alors des instructions inatteignables, et l'agent
        # perdrait en silence des consignes qu'on lui a rattachées. Dans ce cas
        # on retombe sur l'inscription complète.
        # `load_skill` reste reconnu dans une allowlist existante pendant la
        # transition vers le chargeur groupé `load_skills`.
        differe_les_skills = _allows_internal_tool(
            "load_skills", runtime_allowlist
        ) or _allows_internal_tool("load_skill", runtime_allowlist)
        # `implicit_skills` est forcée pour la même raison qu'une skill demandée
        # à l'exécution : c'est un réglage de l'agent qui l'a fait entrer, pas
        # une éventualité que le modèle pourrait reconnaître. Activer la mémoire
        # utilisateur puis laisser le modèle décider s'il la lit reviendrait à
        # ne pas l'activer.
        eager_skills = (
            inlined_skills(
                skills,
                requested_skills,
                forced=[*(runtime_skills or []), *implicit_skills],
            )
            if differe_les_skills
            else list(requested_skills)
        )
        for skill_id in eager_skills:
            instructions.append(render_skill(skills, skill_id))
        workflow_instruction = _render_workflow(workflow)
        if workflow_instruction:
            instructions.append(workflow_instruction)
        toolsets: list[Any] = []
        neutral_toolsets: list[Any] = []
        capabilities: list[Any] = []
        disabled_tools = self.config.disabled_tools(agent_id)
        selected_tools = set(config.declared_tools)
        known_tools = {
            tool.name for manifest, _ in loaded_modules for tool in manifest.tools
        }
        unknown_disabled = disabled_tools - known_tools
        if unknown_disabled:
            raise ConfigurationError(
                f"agent {agent_id} disables unknown tools: {sorted(unknown_disabled)}"
            )
        for manifest, module in loaded_modules:
            instructions.extend(module.instructions())
            risks = {tool.name: tool.risk_tags for tool in manifest.tools}
            timeouts = {tool.name: tool.timeout_seconds for tool in manifest.tools}
            path_parameters = {tool.name: tool.path_parameters for tool in manifest.tools}
            url_parameters = {tool.name: tool.url_parameters for tool in manifest.tools}
            module_toolsets = module.toolsets()
            self._append_toolsets(
                toolsets,
                module_toolsets,
                disabled_tools,
                selected_tools,
                runtime_allowlist,
                agent_id,
                risks,
                timeouts,
                path_parameters,
                url_parameters,
                # Un orchestrateur ne prend pas le travail de ses enfants : le
                # Guardian lui refuse l'écriture hors de son espace personnel.
                # Le sous-agent neutre plus bas ne délègue à personne, donc rien
                # ne le restreint.
                delegates=tuple(config.delegates),
            )
            self._append_toolsets(
                neutral_toolsets,
                module.toolsets(),
                disabled_tools,
                selected_tools,
                runtime_allowlist,
                "subagent",
                risks,
                timeouts,
                path_parameters,
                url_parameters,
            )
            capabilities.extend(module.capabilities())
        catalog = skill_catalog_instruction(skills, requested_skills, eager_skills)
        loader = skill_toolset(skills, eager_skills)
        if catalog and differe_les_skills:
            instructions.append(catalog)
        if loader and differe_les_skills:
            toolsets.append(loader)
        if child_agents and _allows_internal_tool("agent_delegate", runtime_allowlist):
            capabilities.append(make_subagents(config.id, child_agents, budgets))
        overhead = self.context.overhead_tokens(
            config,
            skills,
            workflow_instruction or "",
            workspace,
            inlined_skill_names=eager_skills,
        )
        if user_memory_instruction:
            overhead += self.context.estimate_tokens(user_memory_instruction)
        if catalog and differe_les_skills:
            overhead += self.context.estimate_tokens(catalog)
        capabilities.append(
            ContextWindowCompaction(
                agent_id=config.id,
                context_window_tokens=context_window_tokens,
                all_tool_names=known_tools,
                overhead_tokens=overhead,
                trigger_ratio=trigger_ratio,
                force=force_compaction,
            )
        )
        neutral_agent = Agent(
            provider_factory.build(resolved_provider_id, resolved_model_name),
            name=f"{config.id}_subagent",
            description="Neutral ephemeral subagent with a runtime-assigned role.",
            deps_type=RuntimeDeps,
            instructions=[
                self.config.system_instructions(),
                self.runtime_instruction(
                    active_workspace,
                    active_security,
                    provider_id=resolved_provider_id,
                    model_name=resolved_model_name,
                    context_window_tokens=context_window_tokens,
                    compaction_threshold_ratio=trigger_ratio,
                    # Le sous-agent neutre tient son rôle du prompt : lui donner
                    # l'identité du parent le ferait répondre à sa place.
                    agent_id=f"{config.id}_subagent",
                ),
                self.workspace_maps.build(active_workspace).render(),
                self.context.secret_catalog_instruction(),
                (
                    "You are a neutral ephemeral subagent. Your role, bounded task, "
                    "scope and expected output are supplied in the user prompt. "
                    "Do not expand them, delegate again, or modify the parent plan."
                ),
                *([workflow_instruction] if workflow_instruction else []),
            ],
            toolsets=neutral_toolsets,
            capabilities=[
                ContextWindowCompaction(
                    agent_id="subagent",
                    context_window_tokens=context_window_tokens,
                    all_tool_names=known_tools,
                    overhead_tokens=overhead,
                    trigger_ratio=trigger_ratio,
                )
            ],
            output_type=str,
            max_concurrency=budgets.max_concurrency,
        )
        if _allows_internal_tool("subagent_spawn", runtime_allowlist):
            toolsets.append(make_neutral_subagent_toolset(config.id, neutral_agent, budgets))
        return Agent(
            provider_factory.build(
                provider_override or config.provider,
                model_override or config.model,
            ),
            name=config.id,
            description=config.description,
            deps_type=RuntimeDeps,
            instructions=instructions,
            toolsets=toolsets,
            capabilities=capabilities,
            output_type=[str, DeferredToolRequests],
            max_concurrency=budgets.max_concurrency,
            # Le défaut du SDK est de 1 : un argument mal formé condamnait le run
            # entier au deuxième essai, alors que le modèle avait compris son
            # erreur et savait la corriger. `budgets.retries` était défini mais
            # ne servait qu'aux sous-agents.
            retries=budgets.retries,
        )

    @staticmethod
    def _filtered_toolset(
        toolset,
        disabled: set[str],
        selected: set[str],
        runtime_allowlist: set[str] | None,
    ):
        return FilteredToolset(
            toolset,
            lambda _ctx, tool_def: (
                tool_def.name not in disabled
                and (not selected or tool_def.name in selected)
                and (runtime_allowlist is None or tool_def.name in runtime_allowlist)
            ),
        )

    def _append_toolsets(
        self,
        target: list[Any],
        source: list[Any],
        disabled: set[str],
        selected: set[str],
        runtime_allowlist: set[str] | None,
        agent_id: str,
        risks,
        timeouts,
        path_parameters,
        url_parameters,
        delegates: tuple[str, ...] = (),
    ) -> None:
        for toolset in source:
            target.append(
                GuardianToolset(
                    self._filtered_toolset(
                        toolset,
                        disabled,
                        selected,
                        runtime_allowlist,
                    ),
                    agent_id=agent_id,
                    risks=risks,
                    timeouts=timeouts,
                    path_parameters=path_parameters,
                    url_parameters=url_parameters,
                    delegates=delegates,
                )
            )


def _allows_internal_tool(name: str, runtime_allowlist: set[str] | None) -> bool:
    return runtime_allowlist is None or name in runtime_allowlist


def _render_workflow(workflow: dict[str, Any] | None) -> str | None:
    if workflow is None:
        return None
    return render_workflow_instructions(WorkflowDefinition.model_validate(workflow))
