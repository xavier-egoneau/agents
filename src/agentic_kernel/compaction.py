from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
    ToolCallPart,
)
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

# Préfixe posé par la bibliothèque devant chaque résumé de compaction. Il sert
# à la retrouver dans l'historique; un test vérifie qu'il correspond toujours à
# celui de `pydantic_ai_harness`, faute de quoi le nettoyage ci-dessous
# cesserait silencieusement d'opérer.
PREFIXE_RESUME = "Summary of previous conversation:\n\n"


class ResumeurHorsBudget(SummarizingCompaction):
    """Résume sans consommer le budget de requêtes du run.

    La bibliothèque lance son résumeur avec `usage=ctx.usage` et sans
    `usage_limits` : il partage donc le compteur du run parent tout en héritant
    du plafond par défaut du SDK, `request_limit = 50`. Une fois le run au-delà
    de sa cinquantième requête, la compaction suivante fait tomber l'ensemble —
    sur `UsageLimitExceeded`, en annonçant une limite de 50 alors que la
    configuration en déclare 100.

    Le paradoxe est complet : plus la session compacte, plus elle meurt tôt.
    Mesuré ici, 218 compactions dans une seule session, chacune ajoutant une
    requête au compteur qu'elle est censée soulager.

    Un résumé n'est pas du travail demandé par l'utilisateur, c'est la plomberie
    qui rend ce travail possible. Il ne doit donc peser sur aucun des deux
    budgets. Son coût en tokens sort de l'usage rapporté pour le run, ce qui est
    la contrepartie assumée.
    """

    async def _summarize(  # type: ignore[override]
        self,
        messages: list[Any],
        ctx: Any,
        *,
        previous_summary: str | None = None,
    ) -> str:
        from pydantic_ai import Agent
        from pydantic_ai_harness.compaction._summarizing_compaction import (  # noqa: PLC0415
            _format_messages,
        )

        prompt = self.summary_prompt.format(messages=_format_messages(messages))
        if previous_summary is not None:
            prompt = f"{prompt}\n\n<previous_summary>\n{previous_summary}\n</previous_summary>"
        agent: Agent[None, str] = Agent(
            self.model if self.model is not None else ctx.model,
            instructions=(
                "You are a context summarization assistant. Extract the most "
                "important information from conversations."
            ),
        )
        result = await agent.run(prompt)
        return result.output.strip()


def sans_resumes_perimes(messages: list[Any]) -> list[Any]:
    """Ne garde que le dernier résumé de compaction présent dans l'historique.

    La bibliothèque produit un message fait uniquement de `SystemPromptPart`.
    À la compaction suivante, son extracteur les ramasse tous — l'ancien résumé
    compris — et les réinjecte devant le nouveau. Les résumés s'empilent donc à
    vie : mesuré en production, onze résumés de 1 300 tokens dans un seul
    historique, et une compaction qui ajoutait 1 380 tokens sans en retirer un.

    C'est d'autant plus inutile que le mode incrémental passe déjà le résumé
    précédent au résumeur pour qu'il l'absorbe : on conservait à la fois
    l'ancien et sa version absorbée.
    """
    positions = [
        (index, part)
        for index, message in enumerate(messages)
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, SystemPromptPart) and part.content.startswith(PREFIXE_RESUME)
    ]
    if len(positions) <= 1:
        return messages
    perimes = {id(part) for _, part in positions[:-1]}
    nettoyes: list[Any] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            nettoyes.append(message)
            continue
        gardees = [part for part in message.parts if id(part) not in perimes]
        if len(gardees) == len(message.parts):
            nettoyes.append(message)
        elif gardees:
            nettoyes.append(replace(message, parts=gardees))
    return nettoyes


# Le résumé arrive dans le canal des instructions.
#
# La bibliothèque l'injecte comme un `SystemPromptPart`, préfixé de « Summary of
# previous conversation ». Un document qui s'ouvrait sur « Next actions —
# concrete remaining actions » y était donc lu comme une consigne en cours, et
# pas comme une trace. Observé en production : après un `/compact`, l'agent a
# repris et exécuté la demande précédente au lieu de traiter la nouvelle.
#
# La première ligne dit donc ce que le document est et ce qu'il n'est pas, et
# les titres évitent toute forme impérative.
SUMMARY_PROMPT = """You maintain durable context for an autonomous agent.
The conversation below will be replaced by your summary. Preserve facts; never
invent, generalize away, or silently resolve uncertainty.

This summary is injected into the instructions channel, where it can be mistaken
for a live request. Begin it with exactly this line, then a blank line:

# Record of earlier exchanges — history, not a new request

Everything below that line describes what already happened. It never asks for
anything. Only the user's latest message asks for something.

Write the rest under these exact headings, omitting only empty sections:

## Subject of this conversation
One sentence naming what this conversation is about. This replaces re-injecting
the opening message: a session that has changed subject ten times is described
by its current subject, not by how it started.

## User intent and constraints
The goal, acceptance criteria, preferences, prohibitions, permissions, and
unresolved user requests. Preserve exact wording when precision matters.

## Decisions and rationale
Confirmed decisions and why they were made. Distinguish facts from hypotheses.

## Durable state and artifacts
Exact file paths, identifiers, URLs, commands, configuration values, created or
modified artifacts, process state, and externally visible effects.

## Files already read
Keep the map, drop the bytes. One line per file: path, role, and what it holds
that matters for the rest of the work — exported names, structure, conventions,
known gaps. Do not re-read a file listed here unless there is reason to believe
it changed since.

This section exists because compaction erases the contents of a file while
leaving the agent unaware it ever read it: measured on one session, 256 reads for
31 distinct files, `player.js` read thirty-five times. Each re-read refills the
window and forces the next compaction, which erases it again.

## Tool evidence
Material tool results, errors, exit codes, hashes, citations, and observations
needed to justify later work. Do not preserve secrets.

## Completed work
What is verified complete, including tests and their outcomes.

## Work in progress at the cut-off
What was under way when the conversation was compacted.

## Remaining actions recorded at the cut-off
Concrete remaining actions in dependency order, as they stood at that moment.
This is a record of where the work stopped, not an instruction to resume it: act
on it only if the user's latest message asks for it.

## Open questions and risks
Unknowns, failed assumptions, blockers, and information that must be rechecked.

Prefer exact values over prose. Do not claim success without evidence.

This summary is paid for on every single request until the next compaction, so
length is a running cost, not a one-off. Write fragments, not sentences. Drop
anything the agent can re-derive by reading a file or calling a tool — keep the
path, not the contents. Aim for 400 words; never exceed 800.

Respond only with the summary.

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
        self._last_noop_tokens: int | None = None
        # Only deterministic, read-only observations may be cleared. Mutation,
        # approval, memory, plan, browser and network evidence stays available
        # to the summarizer and remains fully preserved in the JSONL audit.
        #
        # `read` appartient à cette famille et en était pourtant absent. C'est
        # précisément ce qui remplit une session de code : sans lui, le tier ne
        # récupérait rien et la compaction repartait à la requête suivante sans
        # avoir rien réduit — douze fois de suite sur une session observée, avec
        # un historique monté à 130 000 tokens pour une fenêtre de 65 536.
        # Un fichier relu donne le même contenu; le perdre coûte un appel.
        reclaimable = {"list", "read", "stat", "search_text", "process_output"}
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

        La mesure est bidirectionnelle : selon la sérialisation, notre compteur
        UTF-8 peut sous-estimer ou largement surestimer la requête réelle.
        """
        return max(
            0.1,
            min(4.0, float(getattr(deps, "context_calibration", 1.0) or 1.0)),
        )

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
        return self._calibrated_tokens(estimated, calibration) >= math.floor(
            context_window * self.trigger_ratio
        )

    def _calibrated_tokens(self, estimated: int, calibration: float) -> int:
        """Calibrate history without shrinking fixed prompt/tool overhead.

        The calibration sample compares the serialized message history with
        provider usage. Tool schemas and system instructions are a separate,
        fixed cost. Applying test9's 0.27 history factor to that overhead hid
        roughly 6k tokens and allowed a 66,031-token request into a 65,536
        window.
        """
        history = max(0, estimated - self.overhead_tokens)
        return math.ceil(history * calibration) + self.overhead_tokens

    def in_noop_cooldown(
        self, estimated: int, context_window: int | None, calibration: float
    ) -> bool:
        if self.force or self._last_noop_tokens is None:
            return False
        if context_window is not None and self._calibrated_tokens(
            estimated, calibration
        ) >= math.floor(context_window * 0.85):
            # The cooldown saves repeated no-op passes, but it must never
            # suppress the last safety barrier before the provider's hard cap.
            return False
        retry_growth = max(
            4_000,
            math.ceil((context_window or estimated) * 0.05 / calibration),
        )
        return estimated < self._last_noop_tokens + retry_growth

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
        if self.in_noop_cooldown(before, context_window, calibration):
            # Une passe qui n'a rien retiré ne devient pas utile à la requête
            # suivante par magie. Attendre une croissance matérielle évite la
            # boucle « outil → compaction identique → outil » observée 270 fois
            # sur test9, tout en réessayant si le contexte grossit réellement.
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
                math.floor(
                    max(1, context_window * self.target_ratio - self.overhead_tokens) / calibration
                )
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
                ResumeurHorsBudget(
                    max_messages=1,
                    # Une queue mesurée en messages ne veut rien dire : vingt
                    # messages font 3 000 tokens dans une conversation et
                    # 120 000 dans une session de code, où chaque résultat
                    # d'outil pèse un fichier entier. Le seuil était alors
                    # atteint sans qu'il reste quoi que ce soit à résumer.
                    # Mesurée en tokens, la queue s'adapte à ce qu'elle contient.
                    keep_tokens=max(1, math.floor(target * 0.6)),
                    # Le premier message de la session était réinjecté tel quel,
                    # comme un vrai tour utilisateur. Dans une session canonique
                    # qui vit des semaines et change vingt fois de sujet, l'agent
                    # se retrouvait avec une demande périmée présentée comme
                    # actuelle : après un `/compact`, il a repris une recherche
                    # de la veille au lieu de traiter la commande reçue. La
                    # section « Subject of this conversation » du résumé porte
                    # désormais cette information, en une phrase.
                    preserve_first_user_message=False,
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
        request_context.messages = await tiered.compact(
            sans_resumes_perimes(list(request_context.messages)), ctx
        )
        after = self._tokens(request_context.messages)
        self._last_noop_tokens = after if after >= before else None
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
                    "calibrated_tokens_before": self._calibrated_tokens(before, calibration),
                    "calibrated_tokens_after": self._calibrated_tokens(after, calibration),
                    "manual": self.force,
                    "preserves_full_audit": True,
                    "estimator": "utf8_bytes/3.5",
                },
            )
        )
        return request_context
