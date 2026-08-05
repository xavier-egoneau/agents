from __future__ import annotations

import json
import math

from pydantic_ai import RunContext
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai_harness.compaction import (
    ClampOversizedMessages,
    ClearToolResults,
    DeduplicateFileReads,
    SummarizingCompaction,
    TieredCompaction,
)

from .models import Event
from .orchestration import RuntimeDeps

SUMMARY_PROMPT = """You maintain durable context for an autonomous agent.
The conversation below will be replaced by your summary. Preserve facts; never
invent, generalize away, or silently resolve uncertainty.

Write the summary under these exact headings, omitting only empty sections:

## User intent and constraints
The goal, acceptance criteria, preferences, prohibitions, permissions, and
unresolved user requests. Preserve exact wording when precision matters.

## Decisions and rationale
Confirmed decisions and why they were made. Distinguish facts from hypotheses.

## Durable state and artifacts
Exact file paths, identifiers, URLs, commands, configuration values, created or
modified artifacts, process state, and externally visible effects.

## Tool evidence
Material tool results, errors, exit codes, hashes, citations, and observations
needed to justify later work. Do not preserve secrets.

## Completed work
What is verified complete, including tests and their outcomes.

## Current work
What was in progress at the compaction boundary.

## Next actions
Concrete remaining actions in dependency order.

## Open questions and risks
Unknowns, failed assumptions, blockers, and information that must be rechecked.

Prefer exact values over prose. Do not claim success without evidence. Respond
only with the summary.

<messages>
{messages}
</messages>"""


def _file_read_key(call: ToolCallPart) -> str | None:
    if call.tool_name != "read":
        return None
    try:
        value = call.args_as_dict().get("path")
    except Exception:
        return None
    return value if isinstance(value, str) else None


class ContextWindowCompaction(AbstractCapability[RuntimeDeps]):
    """Run Harness tiered compaction at 70%, targeting 50%."""

    def __init__(
        self,
        *,
        agent_id: str,
        context_window_tokens: int | None,
        all_tool_names: set[str],
        overhead_tokens: int = 0,
        trigger_ratio: float = 0.70,
        target_ratio: float = 0.50,
        force: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.context_window_tokens = context_window_tokens
        self.overhead_tokens = overhead_tokens
        self.trigger_ratio = trigger_ratio
        self.target_ratio = target_ratio
        self.force = force
        # Only deterministic, read-only observations may be cleared. Mutation,
        # approval, memory, plan, browser and network evidence stays available
        # to the summarizer and remains fully preserved in the JSONL audit.
        reclaimable = {"list", "stat", "search_text", "process_output"}
        self.protected_tools = frozenset(all_tool_names - reclaimable)

    @staticmethod
    def _estimate_text(value: str) -> int:
        return max(1, math.ceil(len(value.encode("utf-8")) / 3.5))

    def _tokens(self, messages) -> int:
        encoded = json.dumps(
            ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
            ensure_ascii=False,
        )
        return self.overhead_tokens + self._estimate_text(encoded)

    @staticmethod
    def calibration_of(deps: Any) -> float:
        """Écart mesuré entre les tokens facturés et notre estimation.

        Jamais sous 1.0 : un facteur inférieur ferait croire à plus de marge
        qu'il n'y en a, et retarderait encore la compaction.
        """
        return max(1.0, float(getattr(deps, "context_calibration", 1.0) or 1.0))

    def should_compact(
        self, estimated: int, context_window: int | None, calibration: float
    ) -> bool:
        """Décide sur une estimation ramenée à l'échelle du fournisseur.

        L'estimateur compte 3,5 octets par token; sur du code et du JSON, le
        fournisseur en facture deux fois plus. Comparer l'estimation brute au
        seuil revenait à attendre le double du volume voulu — donc à compacter
        une fois la fenêtre déjà dépassée.
        """
        if self.force:
            return True
        if context_window is None:
            return False
        return math.ceil(estimated * calibration) >= math.floor(
            context_window * self.trigger_ratio
        )

    async def before_model_request(
        self,
        ctx: RunContext[RuntimeDeps],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        before = self._tokens(request_context.messages)
        context_window = ctx.deps.context_window_tokens or self.context_window_tokens
        if context_window is None and not self.force:
            return request_context
        calibration = self.calibration_of(ctx.deps)
        if not self.should_compact(before, context_window, calibration):
            return request_context

        if self.force:
            # A manual request must reduce the current live history even when it
            # is already below the automatic 50%-of-window target.
            target = math.floor(max(1, before - self.overhead_tokens) * self.target_ratio)
        else:
            # La cible est exprimée dans l'unité du compacteur, qui mesure avec
            # le même estimateur optimiste. Viser 50 % de la fenêtre sans diviser
            # par le facteur reviendrait à s'arrêter au moment où l'historique
            # réel occupe encore toute la fenêtre : on déclencherait au bon
            # moment sans jamais assez réduire.
            target = (
                math.floor(context_window * self.target_ratio / calibration)
                - self.overhead_tokens
                if context_window is not None
                else math.floor(before * self.target_ratio)
            )
        target = max(1, target)
        per_part_limit = max(
            4_000,
            math.floor((context_window or before) * 0.20),
        )
        # Preserve the exact pre-compaction material in the append-only audit.
        # The live context may be reduced, but the source data remains recoverable.
        exact_messages = ModelMessagesTypeAdapter.dump_python(request_context.messages, mode="json")
        snapshot = (
            ctx.deps.snapshot_store.save(ctx.deps.session_id, exact_messages)
            if ctx.deps.snapshot_store is not None
            else {"messages": exact_messages}
        )
        ctx.deps.events.append(
            Event(
                session_id=ctx.deps.session_id,
                run_id=ctx.deps.root_run_id,
                agent_id=self.agent_id,
                type="context.pre_compaction_snapshot",
                payload={
                    **snapshot,
                    "estimated_tokens": before,
                },
            )
        )
        tiered = TieredCompaction(
            tiers=[
                DeduplicateFileReads(
                    file_key=_file_read_key,
                    tokenizer=self._estimate_text,
                ),
                ClearToolResults(
                    max_tokens=1,
                    keep_pairs=4,
                    exclude_tools=self.protected_tools,
                    min_clear_tokens=2_000,
                    tokenizer=self._estimate_text,
                ),
                SummarizingCompaction(
                    max_messages=1,
                    keep_messages=20,
                    preserve_first_user_message=True,
                    incremental=True,
                    summary_prompt=SUMMARY_PROMPT,
                    tokenizer=self._estimate_text,
                ),
                # Emergency-only final tier: the semantic summary gets the
                # original content first. Clamp only if a recent single part
                # is itself too large to fit.
                ClampOversizedMessages(
                    max_part_tokens=per_part_limit,
                    tokenizer=self._estimate_text,
                ),
            ],
            target_tokens=target,
            tokenizer=self._estimate_text,
        )
        request_context.messages = await tiered.compact(list(request_context.messages), ctx)
        after = self._tokens(request_context.messages)
        ctx.deps.events.append(
            Event(
                session_id=ctx.deps.session_id,
                run_id=ctx.deps.root_run_id,
                agent_id=self.agent_id,
                type="context.compacted",
                payload={
                    "scope": "active_run",
                    "strategy": "tiered_harness",
                    "estimated_tokens_before": before,
                    "estimated_tokens_after": after,
                    "context_window_tokens": context_window,
                    "trigger_tokens": (
                        math.floor(context_window * self.trigger_ratio)
                        if context_window is not None
                        else None
                    ),
                    "target_tokens": target + self.overhead_tokens,
                    "threshold_ratio": self.trigger_ratio,
                    # Ce que valent réellement `before` et `after` chez le
                    # fournisseur : sans ce facteur, la trace reproduit
                    # l'optimisme de l'estimateur.
                    "calibration_factor": calibration,
                    "calibrated_tokens_before": math.ceil(before * calibration),
                    "calibrated_tokens_after": math.ceil(after * calibration),
                    "manual": self.force,
                    "preserves_full_audit": True,
                    "estimator": "utf8_bytes/3.5",
                },
            )
        )
        return request_context
