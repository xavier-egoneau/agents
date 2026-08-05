from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.toolsets import FilteredToolset

from .compaction import ContextWindowCompaction
from .errors import ConfigurationError
from .guardian import GuardianToolset
from .models import SecurityMode
from .orchestration import RuntimeDeps, make_neutral_subagent_toolset, make_subagents
from .skills import render_skill, skill_catalog_instruction, skill_toolset
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

    def build(
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
        resolved_model_name = (
            model_override
            or config.model
            or provider_factory.get_config(resolved_provider_id).model
        )
        context_window_tokens = self.context_registry.get(
            resolved_provider_id, resolved_model_name
        )
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
            ),
            self.workspace_maps.build(active_workspace).render(),
            self.context.secret_catalog_instruction(),
            config.instructions,
        ]
        if user_memory_instruction:
            instructions.append(user_memory_instruction)
        for skill_id in requested_skills:
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
        catalog = skill_catalog_instruction(skills)
        loader = skill_toolset(skills)
        if catalog and _allows_internal_tool("load_skill", runtime_allowlist):
            instructions.append(catalog)
        if loader and _allows_internal_tool("load_skill", runtime_allowlist):
            toolsets.append(loader)
        if child_agents and _allows_internal_tool("agent_delegate", runtime_allowlist):
            capabilities.append(make_subagents(config.id, child_agents, budgets))
        overhead = self.context.overhead_tokens(
            config,
            skills,
            workflow_instruction or "",
            workspace,
        )
        if user_memory_instruction:
            overhead += self.context.estimate_tokens(user_memory_instruction)
        capabilities.append(
            ContextWindowCompaction(
                agent_id=config.id,
                context_window_tokens=context_window_tokens,
                all_tool_names=known_tools,
                overhead_tokens=overhead,
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
                )
            )


def _allows_internal_tool(name: str, runtime_allowlist: set[str] | None) -> bool:
    return runtime_allowlist is None or name in runtime_allowlist


def _render_workflow(workflow: dict[str, Any] | None) -> str | None:
    if workflow is None:
        return None
    return render_workflow_instructions(WorkflowDefinition.model_validate(workflow))
