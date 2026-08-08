from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import Agent, FunctionToolset, ModelRetry, RunContext
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.subagents import SubAgent, SubAgents
from pydantic_ai_harness.subagents import SubAgentToolset as HarnessSubAgentToolset

from .errors import BudgetExceeded
from .events import JsonlEventStore
from .guardian import action_family
from .models import ApprovalRequest, BudgetConfig, Event, GuardianDecision, SecurityMode
from .plans import PlanConflict, PlanNotFound, PlanService
from .trace_context import bind_event_run_id, event_run_id


@dataclass
class RuntimeDeps:
    session_id: UUID
    root_run_id: UUID
    budgets: BudgetConfig
    events: JsonlEventStore
    semaphore: asyncio.Semaphore
    workspace: Path = field(default_factory=Path.cwd)
    security_mode: SecurityMode = SecurityMode.LIMITED
    pending_approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    approved_scopes: set[tuple[str, str, str | None]] = field(default_factory=set)
    agent_runs: int = 1
    attempts: dict[str, int] = field(default_factory=dict)
    tool_catalog: list[dict[str, Any]] = field(default_factory=list)
    state_db: Path | None = None
    provider_id: str | None = None
    model_name: str | None = None
    context_window_tokens: int | None = None
    # Rapport entre les tokens réellement facturés par le fournisseur et notre
    # estimation, mesuré sur les tours précédents de la session. L'estimateur
    # compte 3,5 octets par token : sur du code, du JSON ou des identifiants,
    # c'est deux fois trop optimiste. Sans ce correctif, la compaction décide
    # sur un chiffre qui n'a plus de rapport avec la fenêtre réelle.
    context_calibration: float = 1.0
    # Agent racine du run, pas l'agent courant. La bibliothèque de connaissance
    # appartient à l'orchestrateur : un sous-agent appelé par `main` doit lire
    # et écrire dans celle de `main`, jamais dans une sienne — sinon ce qu'il
    # apprend se perd à la fin de la délégation.
    orchestrator_id: str = "main"
    secret_resolver: Callable[[str], str | None] | None = None
    secret_redactor: Callable[[Any], Any] | None = None
    snapshot_store: Any | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_scope_approved(self, decision: GuardianDecision) -> bool:
        return (
            decision.tool_name,
            action_family(decision.risks),
            decision.path,
        ) in self.approved_scopes

    async def reserve_run(self, agent_id: str = "unknown") -> int:
        async with self.lock:
            if self.agent_runs >= self.budgets.max_agent_runs:
                raise BudgetExceeded(
                    f"session agent-run limit reached ({self.budgets.max_agent_runs})"
                )
            self.agent_runs += 1
            self.attempts[agent_id] = self.attempts.get(agent_id, 0) + 1
            return self.attempts[agent_id] - 1


class TracedSubAgentToolset(HarnessSubAgentToolset[RuntimeDeps]):
    def __init__(self, *, parent_agent_id: str, **kwargs: Any) -> None:
        self.parent_agent_id = parent_agent_id
        super().__init__(**kwargs)

    @staticmethod
    def _claim_plan_step(
        deps: RuntimeDeps,
        agent_name: str,
        plan_id: str | None,
        step_id: str | None,
        child_run_id: UUID,
    ) -> None:
        """Réserve la tâche du plan avant de lancer l'agent.

        C'est cette réservation qui autorise le parallélisme : `plan_claim`
        refuse une tâche parallèle dépourvue de périmètre d'écriture, et refuse
        celle dont le périmètre recouvre une tâche déjà en cours. Sans elle,
        deux agents pouvaient écrire au même endroit sans que rien ne
        l'empêche — le mécanisme n'était atteignable que par l'exécutant neutre.
        """
        if not plan_id and not step_id:
            return
        if bool(plan_id) != bool(step_id):
            raise ModelRetry("plan_id et step_id doivent être fournis ensemble.")
        if deps.state_db is None:
            raise ModelRetry("La base de plans est indisponible : exécute la tâche sans plan.")
        try:
            PlanService(deps.state_db).claim(
                str(plan_id),
                str(step_id),
                session_id=deps.session_id,
                # `agent:<nom>` se relit; `subagent:<ce que le modèle a tapé>`
                # dépendait de son humeur du jour.
                claimed_by=f"agent:{agent_name}",
                run_id=str(child_run_id),
                lease_seconds=max(30, int(deps.budgets.child_timeout_seconds)),
            )
        except (PlanNotFound, PlanConflict) as exc:
            # `ModelRetry` plutôt qu'une exception : un conflit de périmètre est
            # une information exploitable — le modèle doit choisir une autre
            # tâche, pas interrompre le run.
            raise ModelRetry(f"Tâche {step_id} non réservable : {exc}") from exc

    async def delegate_task(
        self,
        ctx: RunContext[RuntimeDeps],
        agent_name: str,
        task: str,
        plan_id: str | None = None,
        step_id: str | None = None,
    ) -> str:
        """Confie une tâche autonome à un agent configuré et renvoie son résultat.

        L'agent s'exécute dans un contexte neuf et ne voit pas cette
        conversation : `task` doit contenir tout ce dont il a besoin.

        Args:
            ctx: contexte d'exécution du parent.
            agent_name: nom de l'agent, parmi ceux listés dans les instructions.
            task: consigne complète et autonome.
            plan_id: plan auquel rattacher l'exécution, avec `step_id`.
            step_id: tâche du plan à réserver avant de commencer. Fournir les
                deux fait prendre le bail et vérifier les conflits de périmètre
                d'écriture, ce qui autorise l'exécution en parallèle.
        """
        deps: RuntimeDeps = ctx.deps
        child_run_id = uuid4()
        # La réservation précède tout événement : échouer après avoir annoncé le
        # démarrage laisserait la trace d'un travail jamais entrepris.
        self._claim_plan_step(deps, agent_name, plan_id, step_id, child_run_id)
        attempt = await deps.reserve_run(agent_name)
        parent_run_id = event_run_id(deps.root_run_id)
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id=agent_name,
                parent_run_id=parent_run_id,
                type="agent.queued",
                attempt=attempt,
                payload={
                    "task": task,
                    "delegated_by": self.parent_agent_id,
                    "plan_id": plan_id,
                    "step_id": step_id,
                },
            )
        )
        async with deps.semaphore:
            deps.events.append(
                Event(
                    session_id=deps.session_id,
                    run_id=child_run_id,
                    agent_id=agent_name,
                    parent_run_id=parent_run_id,
                    type="agent.started",
                    attempt=attempt,
                    payload={"task": task, "delegated_by": self.parent_agent_id},
                )
            )
            try:
                with bind_event_run_id(child_run_id):
                    output = await super().delegate_task(ctx, agent_name, task)
            except Exception as exc:
                deps.events.append(
                    Event(
                        session_id=deps.session_id,
                        run_id=child_run_id,
                        agent_id=agent_name,
                        parent_run_id=parent_run_id,
                        type="agent.failed",
                        attempt=attempt,
                        payload={"error_type": type(exc).__name__, "message": str(exc)},
                    )
                )
                raise
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id=agent_name,
                parent_run_id=parent_run_id,
                type="agent.completed",
                attempt=attempt,
                payload={"output": output},
            )
        )
        return output


class TracedSubAgents(SubAgents[RuntimeDeps]):
    def __init__(self, *, parent_agent_id: str, **kwargs: Any) -> None:
        self.parent_agent_id = parent_agent_id
        super().__init__(**kwargs)

    def get_toolset(self):
        if not self._by_name:
            return None
        return TracedSubAgentToolset(
            parent_agent_id=self.parent_agent_id,
            agents=self._by_name,
            forward_usage=self.forward_usage,
            inherit_tools=self.inherit_tools,
            shared_capabilities=self.shared_capabilities,
            event_stream_handler=self.event_stream_handler,
            tool_name=self.tool_name,
            tool_retries=self.tool_retries,
            contain_errors=self.contain_errors,
            call_counts=self._call_counts,
        )


def make_subagents(
    parent_agent_id: str,
    agents: list[Agent[RuntimeDeps, Any]],
    budgets: BudgetConfig,
) -> TracedSubAgents:
    return TracedSubAgents(
        parent_agent_id=parent_agent_id,
        agents=[
            SubAgent(
                agent,
                usage_limits=UsageLimits(request_limit=budgets.max_requests_per_agent),
                timeout_seconds=budgets.child_timeout_seconds,
                max_calls=budgets.max_agent_runs,
                contain_errors=True,
            )
            for agent in agents
        ],
        agent_folders=None,
        forward_usage=True,
        tool_retries=budgets.retries,
        contain_errors=True,
        tool_name="agent_delegate",
    )


def make_neutral_subagent_toolset(
    parent_agent_id: str,
    agent: Agent[RuntimeDeps, Any],
    budgets: BudgetConfig,
) -> FunctionToolset[RuntimeDeps]:
    """Expose an ephemeral neutral subagent whose role is supplied per call."""

    async def subagent_spawn(
        ctx: RunContext[RuntimeDeps],
        role: str,
        task: str,
        expected_output: str,
        scope: list[str] | None = None,
        plan_id: str | None = None,
        step_id: str | None = None,
        justification: str = "",
    ) -> dict[str, Any]:
        if not role.strip() or not task.strip() or not expected_output.strip():
            raise ValueError("role, task and expected_output are required")
        deps = ctx.deps
        attempt = await deps.reserve_run("subagent")
        child_run_id = uuid4()
        parent_run_id = event_run_id(deps.root_run_id)
        if bool(plan_id) != bool(step_id):
            raise ValueError("plan_id and step_id must be supplied together")
        if plan_id and step_id:
            if deps.state_db is None:
                raise ValueError("plan state database is unavailable")
            try:
                PlanService(deps.state_db).claim(
                    plan_id,
                    step_id,
                    session_id=deps.session_id,
                    claimed_by=f"subagent:{role.strip()}",
                    run_id=str(child_run_id),
                    lease_seconds=max(30, int(budgets.child_timeout_seconds)),
                )
            except (PlanNotFound, PlanConflict) as exc:
                return {
                    "ok": False,
                    "data": None,
                    "error": {"type": "plan_conflict", "message": str(exc)},
                    "metadata": {
                        "run_id": str(child_run_id),
                        "plan_id": plan_id,
                        "step_id": step_id,
                    },
                }
        prompt = (
            f"# Temporary role\n{role.strip()}\n\n"
            f"# Bounded task\n{task.strip()}\n\n"
            f"# Allowed scope\n{', '.join(scope or ['workspace'])}\n\n"
            f"# Expected output\n{expected_output.strip()}\n\n"
            "Stay within this task and scope. Report files changed, evidence, "
            "validation and blockers. Do not redefine your permissions."
        )
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id="subagent",
                parent_run_id=parent_run_id,
                type="agent.queued",
                attempt=attempt,
                payload={
                    "role": role,
                    "task": task,
                    "scope": scope or [],
                    "expected_output": expected_output,
                    "delegated_by": parent_agent_id,
                    "ephemeral": True,
                    "plan_id": plan_id,
                    "step_id": step_id,
                },
            )
        )
        async with deps.semaphore:
            deps.events.append(
                Event(
                    session_id=deps.session_id,
                    run_id=child_run_id,
                    agent_id="subagent",
                    parent_run_id=parent_run_id,
                    type="agent.started",
                    attempt=attempt,
                    payload={
                        "role": role,
                        "task": task,
                        "scope": scope or [],
                        "expected_output": expected_output,
                        "delegated_by": parent_agent_id,
                        "ephemeral": True,
                        "plan_id": plan_id,
                        "step_id": step_id,
                    },
                )
            )
            try:
                with bind_event_run_id(child_run_id):
                    result = await agent.run(
                        prompt,
                        deps=deps,
                        usage_limits=UsageLimits(request_limit=budgets.max_requests_per_agent),
                    )
            except Exception as exc:
                deps.events.append(
                    Event(
                        session_id=deps.session_id,
                        run_id=child_run_id,
                        agent_id="subagent",
                        parent_run_id=parent_run_id,
                        type="agent.failed",
                        attempt=attempt,
                        payload={
                            "role": role,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        },
                    )
                )
                if plan_id and step_id and deps.state_db is not None:
                    PlanService(deps.state_db).update(
                        plan_id,
                        step_id,
                        "failed",
                        session_id=deps.session_id,
                        note=str(exc),
                    )
                return {
                    "ok": False,
                    "data": None,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    "metadata": {"run_id": str(child_run_id)},
                }
        output = str(result.output)
        if plan_id and step_id and deps.state_db is not None:
            PlanService(deps.state_db).update(
                plan_id,
                step_id,
                "validating",
                session_id=deps.session_id,
                note="Subagent returned a result; parent validation required.",
            )
        deps.events.append(
            Event(
                session_id=deps.session_id,
                run_id=child_run_id,
                agent_id="subagent",
                parent_run_id=parent_run_id,
                type="agent.completed",
                attempt=attempt,
                payload={
                    "role": role,
                    "output": output,
                    "ephemeral": True,
                    "plan_id": plan_id,
                    "step_id": step_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"role": role, "output": output},
            "error": None,
            "metadata": {
                "run_id": str(child_run_id),
                "plan_id": plan_id,
                "step_id": step_id,
                "requires_parent_validation": bool(plan_id),
            },
        }

    return FunctionToolset(tools=[subagent_spawn])
