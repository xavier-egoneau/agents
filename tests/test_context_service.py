import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_kernel.context_service import (
    ModelContextRegistry,
    without_ephemeral_skill_loads,
)
from agentic_kernel.errors import ConfigurationError
from agentic_kernel.models import Event


def test_context_registry_parses_human_sizes() -> None:
    assert ModelContextRegistry.parse_size("128k") == 128_000
    assert ModelContextRegistry.parse_size("1.5m") == 1_500_000
    assert ModelContextRegistry.parse_size("32_768") == 32_768


@pytest.mark.parametrize("value", ["", "unknown", "100", "20m"])
def test_context_registry_rejects_invalid_sizes(value: str) -> None:
    with pytest.raises(ValueError):
        ModelContextRegistry.parse_size(value)


def test_context_registry_replaces_one_model_without_losing_others(tmp_path: Path) -> None:
    registry = ModelContextRegistry(tmp_path)
    registry.set("deepseek", "chat", 64_000)
    registry.set("openai", "codex", 200_000)
    registry.set("deepseek", "chat", 128_000)

    assert registry.get("deepseek", "chat") == 128_000
    assert registry.get("openai", "codex") == 200_000
    document = json.loads((tmp_path / "models-infos.json").read_text(encoding="utf-8"))
    assert len(document["models"]) == 2


def test_context_registry_reports_invalid_document(tmp_path: Path) -> None:
    (tmp_path / "models-infos.json").write_text("{", encoding="utf-8")
    registry = ModelContextRegistry(tmp_path)
    with pytest.raises(ConfigurationError, match="registre invalide"):
        registry.set("deepseek", "chat", 128_000)


def test_run_cumulative_usage_never_drives_context_gauge_or_calibration(
    project: Path,
) -> None:
    """Le total d'un run de 38 requêtes n'est pas une fenêtre de 715k tokens."""
    from agentic_kernel.kernel import Kernel

    kernel = Kernel(project)
    session_id = uuid4()
    run_id = uuid4()
    kernel.context_registry.set("test", "test-model", 65_536)
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="session.started",
            payload={"prompt": "continue", "workspace": str(project)},
        )
    )
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="messages.snapshot",
            payload={"estimated_tokens": 12_000},
        )
    )
    kernel.events.append(
        Event(
            session_id=session_id,
            run_id=run_id,
            agent_id="main",
            type="session.completed",
            payload={
                "status": "success",
                "output": "done",
                "usage": {"requests": 38, "input_tokens": 714_906},
            },
        )
    )

    projected = kernel.events.projection.context(session_id)
    status = kernel.context_status(
        session_id=session_id,
        provider_id="test",
        model_name="test-model",
    )

    assert projected is not None
    assert projected["observed_input_tokens"] is None
    assert projected["last_run_total_input_tokens"] == 714_906
    assert projected["calibration_factor"] == 1.0
    assert projected["calibration_samples"] == 0
    assert status["measurement"] == "estimated"
    assert status["estimated_request_tokens"] < 65_536
    assert status["estimated_ratio"] == (status["estimated_request_tokens"] / 65_536)


def test_loaded_skill_bodies_do_not_leak_into_the_next_run() -> None:
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    messages = [
        ModelResponse(
            parts=[
                TextPart("I will admit the relevant skill."),
                ToolCallPart("load_skills", {"names": ["review"], "reason": "Needed"}, "s1"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart("load_skills", "VERY LARGE SKILL BODY", "s1"),
                UserPromptPart(content="Continue"),
            ]
        ),
    ]

    cleaned = without_ephemeral_skill_loads(messages)

    assert len(cleaned) == 2
    assert [part.content for part in cleaned[0].parts if isinstance(part, TextPart)] == [
        "I will admit the relevant skill."
    ]
    assert [part.content for part in cleaned[1].parts if isinstance(part, UserPromptPart)] == [
        "Continue"
    ]
    assert "VERY LARGE SKILL BODY" not in repr(cleaned)


def _capacite(window: int):
    from agentic_kernel.compaction import ContextWindowCompaction

    return ContextWindowCompaction(
        agent_id="main", context_window_tokens=window, all_tool_names=set()
    )


def test_compaction_triggered_too_late_without_calibration() -> None:
    """Le défaut observé : 19 compactions et une fenêtre saturée quand même.

    L'estimateur compte 3,5 octets par token; le fournisseur en a facturé 2,4
    fois plus sur une session de code. Comparer l'estimation brute au seuil
    faisait attendre le double du volume voulu, donc au-delà de la fenêtre.
    """
    capacite = _capacite(1_000_000)
    # 500 000 tokens estimés : la moitié de la fenêtre en apparence, donc
    # tranquillement sous le seuil de 700 000.
    estime = 500_000

    assert capacite.should_compact(estime, 1_000_000, 1.0) is False
    # Le facteur relevé sur la session en cause est de 2,43 : ces 500 000
    # tokens en valent 1 215 000 chez le fournisseur, soit plus que la fenêtre
    # entière. La compaction doit avoir eu lieu bien avant.
    assert capacite.should_compact(estime, 1_000_000, 2.43) is True


def test_calibration_accepts_measured_over_and_under_estimation() -> None:
    """Une paire comparable peut corriger l'estimateur dans les deux sens."""
    capacite = _capacite(100_000)

    assert capacite.calibration_of(type("D", (), {"context_calibration": 0.2})()) == 0.2
    assert capacite.calibration_of(type("D", (), {"context_calibration": None})()) == 1.0
    assert capacite.calibration_of(object()) == 1.0
    assert capacite.calibration_of(type("D", (), {"context_calibration": 2.4})()) == 2.4


def test_history_calibration_does_not_shrink_fixed_tool_overhead() -> None:
    from agentic_kernel.compaction import ContextWindowCompaction

    capacite = ContextWindowCompaction(
        agent_id="main",
        context_window_tokens=65_536,
        all_tool_names=set(),
        overhead_tokens=8_000,
        trigger_ratio=0.9,
    )

    # 190k estimated history tokens become 51.3k provider tokens at 0.27,
    # then the fixed 8k schemas still count in full: 59.3k, above 90%.
    assert capacite._calibrated_tokens(198_000, 0.27) == 59_300  # noqa: SLF001
    assert capacite.should_compact(198_000, 65_536, 0.27) is True


def test_a_manual_compaction_ignores_the_threshold() -> None:
    from agentic_kernel.compaction import ContextWindowCompaction

    forcee = ContextWindowCompaction(
        agent_id="main", context_window_tokens=1_000_000, all_tool_names=set(), force=True
    )

    assert forcee.should_compact(10, 1_000_000, 1.0) is True


def test_a_noop_compaction_has_a_growth_cooldown() -> None:
    capacite = _capacite(65_536)
    capacite._last_noop_tokens = 40_000  # noqa: SLF001 - état du circuit breaker

    assert capacite.in_noop_cooldown(42_000, 65_536, 1.0) is True
    assert capacite.in_noop_cooldown(44_000, 65_536, 1.0) is False


def test_noop_cooldown_stops_an_emergency_retry_loop() -> None:
    capacite = _capacite(65_536)
    capacite._last_noop_tokens = 57_000  # noqa: SLF001

    assert capacite.in_noop_cooldown(58_000, 65_536, 1.0) is True


def test_an_unknown_window_cannot_trigger_a_threshold() -> None:
    assert _capacite(0).should_compact(10_000_000, None, 3.0) is False


def test_the_internal_catalog_is_not_counted_as_prompt(project: Path) -> None:
    """L'overhead ne doit compter que ce qui part vers le modèle.

    `tools/index.json` est un catalogue interne — sources, catégories, schémas
    de sortie, métadonnées — et pesait 246 ko chez un utilisateur, soit 70 500
    tokens. Le compter en entier plaçait l'occupation au-dessus du seuil de
    compaction avant même le premier message, rendait la cible de réduction
    négative donc inatteignable, et faisait payer à chaque run un instantané et
    une passe de compaction qui ne réduisaient rien.

    Ce qui part réellement, c'est le nom, la description et le schéma d'entrée
    de chaque outil exposé.
    """
    from agentic_kernel.kernel import Kernel

    kernel = Kernel(project)
    remplissage = {
        "type": "object",
        "properties": {
            f"champ_{i}": {"type": "string", "description": "x" * 200} for i in range(60)
        },
    }
    kernel.module_registry.index_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "modules": [
                    {
                        "schema_version": 1,
                        "id": "clock",
                        "name": "Clock",
                        "description": "Test module",
                        "version": "1.0.0",
                        "entrypoint": "module.py:module",
                        "capabilities": ["tools"],
                        "enabled": True,
                        "config": None,
                        "tools": [
                            {
                                "name": "now",
                                "description": "Return the time.",
                                "category": "test",
                                "risk_tags": ["read"],
                                "timeout_seconds": 5,
                                "input_schema": {"type": "object", "properties": {}},
                                "output_schema": remplissage,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalogue = kernel.context.estimate_tokens(
        kernel.module_registry.index_path.read_text(encoding="utf-8")
    )
    agent = kernel.config.agents()["main"]

    overhead = kernel.context.overhead_tokens(agent, {}, "", project)

    # Le schéma de sortie, que le modèle ne voit jamais, domine le fichier.
    assert catalogue > 3_000
    assert overhead < catalogue / 3
    # Mais l'outil exposé pèse : l'ignorer ferait sous-estimer dans l'autre sens.
    assert overhead > kernel.context.estimate_tokens(
        kernel.config.system_instructions() + agent.instructions
    )


def test_a_file_read_can_be_reclaimed_from_the_context() -> None:
    """Sans `read`, la compaction n'avait rien à récupérer dans une session de code.

    Les résultats de `read` sont ce qui remplit le contexte quand on travaille
    sur du code. Ils étaient pourtant protégés, alors qu'ils remplissent le
    critère annoncé : une observation déterministe, en lecture seule, qui rend
    le même contenu si on la rejoue. Sur une session observée, douze compactions
    d'affilée n'ont rien réduit et l'historique est monté à 130 000 tokens pour
    une fenêtre de 65 536.
    """
    from agentic_kernel.compaction import ContextWindowCompaction

    capacite = ContextWindowCompaction(
        agent_id="main",
        context_window_tokens=65_536,
        all_tool_names={"read", "write", "patch", "command_run", "list", "stat"},
    )

    assert "read" not in capacite.protected_tools
    # Les mutations et les sorties non reproductibles restent protégées.
    assert {"write", "patch", "command_run"} <= capacite.protected_tools


def test_the_summary_announces_itself_as_history() -> None:
    """Le résumé de compaction entre par le canal des instructions.

    La bibliothèque l'injecte comme `SystemPromptPart`. Un document ouvert sur
    « Next actions — concrete remaining actions in dependency order » y était lu
    comme une consigne en cours : après un `/compact`, l'agent a repris la
    demande précédente au lieu de traiter la nouvelle. Le résumé doit donc
    annoncer sa nature avant son contenu, et aucun titre ne doit être impératif.
    """
    from agentic_kernel.compaction import SUMMARY_PROMPT

    assert "# Record of earlier exchanges — history, not a new request" in SUMMARY_PROMPT
    assert "Only the user's latest message asks for something." in SUMMARY_PROMPT
    assert "not an instruction to resume it" in SUMMARY_PROMPT
    # Le titre impératif d'origine ne doit pas revenir par une réécriture.
    assert "## Next actions" not in SUMMARY_PROMPT


def test_the_summary_carries_the_subject_instead_of_the_opening_message() -> None:
    """Le premier message de la session n'est plus réinjecté tel quel.

    Il revenait comme un vrai tour utilisateur. Dans une session canonique qui
    vit des semaines et change vingt fois de sujet, l'agent recevait une demande
    périmée présentée comme actuelle — et a repris une recherche de la veille au
    lieu de traiter la commande qu'on venait de lui donner. Une phrase de sujet
    dans le résumé porte la même information pour bien moins cher.
    """
    from agentic_kernel.compaction import SUMMARY_PROMPT

    assert "## Subject of this conversation" in SUMMARY_PROMPT
    assert "One sentence naming what this conversation is about" in SUMMARY_PROMPT


def test_the_summary_is_told_that_its_length_is_a_running_cost() -> None:
    """Un résumé se paie à chaque requête jusqu'à la compaction suivante."""
    from agentic_kernel.compaction import SUMMARY_PROMPT

    assert "paid for on every single request" in SUMMARY_PROMPT
    assert "never exceed 800" in SUMMARY_PROMPT


def test_the_summary_prefix_still_matches_the_library() -> None:
    """Le nettoyage des résumés dépend de ce préfixe : s'il change, il cesse d'opérer."""
    from pydantic_ai_harness.compaction._summarizing_compaction import _SUMMARY_PREFIX

    from agentic_kernel.compaction import PREFIXE_RESUME

    assert PREFIXE_RESUME == _SUMMARY_PREFIX


def test_only_the_latest_compaction_summary_survives() -> None:
    """La compaction empilait ses résumés au lieu de les remplacer.

    Le message qu'elle produit ne contient que des `SystemPromptPart`; son
    extracteur les ramasse tous à la passe suivante et les réinjecte devant le
    nouveau résumé. Mesuré en production : onze résumés de 1 300 tokens dans un
    seul historique, et une compaction qui ajoutait 1 380 tokens sans en retirer
    un seul. Le mode incrémental ayant déjà absorbé l'ancien dans le nouveau,
    le conserver revenait à le garder en double.
    """
    from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

    from agentic_kernel.compaction import PREFIXE_RESUME, sans_resumes_perimes

    socle = SystemPromptPart(content="Instructions du système.")
    ancien = SystemPromptPart(content=f"{PREFIXE_RESUME}Vieux résumé.")
    recent = SystemPromptPart(content=f"{PREFIXE_RESUME}Résumé courant.")
    messages = [
        ModelRequest(parts=[socle, ancien, recent]),
        ModelRequest(parts=[UserPromptPart(content="Et maintenant ?")]),
    ]

    nettoyes = sans_resumes_perimes(messages)

    restants = [
        part for message in nettoyes for part in message.parts if isinstance(part, SystemPromptPart)
    ]
    assert [part.content for part in restants] == [socle.content, recent.content]
    # La demande de l'utilisateur n'est pas touchée.
    assert len(nettoyes) == 2


def test_a_history_without_summary_is_left_alone() -> None:
    """Sans empilement, rien à nettoyer : on ne recopie pas l'historique pour rien."""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from agentic_kernel.compaction import sans_resumes_perimes

    messages = [ModelRequest(parts=[UserPromptPart(content="Bonjour")])]

    assert sans_resumes_perimes(messages) is messages


def test_the_summarizer_does_not_spend_the_run_request_budget() -> None:
    """La compaction consommait le budget qu'elle est censée soulager.

    La bibliothèque lance son résumeur avec `usage=ctx.usage` et sans
    `usage_limits` : compteur du run partagé, plafond par défaut du SDK à 50.
    Passé la cinquantième requête d'un run, la compaction suivante le tuait sur
    `UsageLimitExceeded` en annonçant 50 alors que la configuration déclare 100.
    Plus une session compactait, plus elle mourait tôt — 218 compactions
    observées dans une seule session.
    """
    import inspect

    from pydantic_ai_harness.compaction._summarizing_compaction import SummarizingCompaction

    from agentic_kernel.compaction import ResumeurHorsBudget

    # Le défaut de la bibliothèque, qu'on contourne, ne doit pas changer sans
    # qu'on le remarque.
    assert "usage=ctx.usage" in inspect.getsource(SummarizingCompaction._summarize)
    assert "usage=ctx.usage" not in inspect.getsource(ResumeurHorsBudget._summarize)


def test_the_kernel_uses_the_off_budget_summarizer() -> None:
    """Sous-classer ne sert à rien si le tier monté reste celui de la bibliothèque."""
    import inspect

    from agentic_kernel import compaction

    monte = inspect.getsource(compaction.ContextWindowCompaction.before_model_request)
    assert "ResumeurHorsBudget(" in monte
    assert "SummarizingCompaction(" not in monte


def test_the_summary_keeps_the_map_of_files_already_read() -> None:
    """La compaction efface le contenu d'un fichier sans dire qu'il a été lu.

    L'agent le relit donc, ce qui remplit la fenêtre et force la compaction
    suivante, qui l'efface à nouveau. Mesuré sur une session : 256 lectures pour
    31 fichiers distincts, `player.js` lu trente-cinq fois. La déduplication ne
    peut rien y faire — elle ne voit que la fenêtre courante, et la lecture
    précédente en a déjà été retirée.

    Garder le chemin et ce qu'on y a trouvé, sans les octets, donne au modèle
    l'information qui lui manquait pour décider de ne pas relire.
    """
    from agentic_kernel.compaction import SUMMARY_PROMPT

    assert "## Files already read" in SUMMARY_PROMPT
    assert "Keep the map, drop the bytes." in SUMMARY_PROMPT
    assert "Do not re-read a file listed here" in SUMMARY_PROMPT
