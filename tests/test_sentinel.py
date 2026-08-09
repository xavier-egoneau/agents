from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_sentinel():
    source = Path(__file__).parents[1] / "tools/modules/sentinel/module.py"
    spec = importlib.util.spec_from_file_location("test_sentinel_tool_module", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Le registre de l'application charge les modules d'outils de la même
    # façon ; l'inscription reproduit ses conditions exactes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sentinel = load_sentinel()


def _finding(**overrides):
    base = {
        "section": "persistence",
        "kind": "registry_run",
        "label": "HKCU\\Run · Updater",
        "identity": ("HKCU\\Run", "Updater", "C:\\Temp\\u.exe"),
    }
    return sentinel.Finding(**{**base, **overrides})


def test_a_fingerprint_survives_a_restart() -> None:
    """L'empreinte ne porte que sur l'identité.

    Y mêler le PID ou l'horodatage ferait paraître neuve, à chaque redémarrage,
    une machine qui n'a pas bougé — et le rapport quotidien deviendrait un mur
    de faux positifs qu'on cesse de lire.
    """
    assert _finding().fingerprint == _finding().fingerprint
    assert _finding().fingerprint != _finding(identity=("HKCU\\Run", "Autre", "x")).fingerprint


def test_an_acknowledged_finding_never_resurfaces() -> None:
    """Un écart validé une fois ne doit plus jamais remonter.

    Sinon la routine finit ignorée en bloc, et c'est le jour où elle avait
    raison qu'on ne la lit pas.
    """
    observation = _finding()
    etat = {
        "baseline": {},
        "acknowledged": {observation.fingerprint: {"note": "mon updater", "at": "2026-01-01"}},
    }

    classees = sentinel.classify([observation], etat)

    assert classees["new"] == []
    assert len(classees["acknowledged"]) == 1


def test_a_known_finding_is_not_reported_as_new() -> None:
    observation = _finding()
    etat = {"baseline": {observation.fingerprint: "2026-01-01"}, "acknowledged": {}}

    classees = sentinel.classify([observation], etat)

    assert classees["new"] == []
    assert len(classees["known"]) == 1


def test_an_unknown_finding_is_new() -> None:
    classees = sentinel.classify([_finding()], {"baseline": {}, "acknowledged": {}})

    assert len(classees["new"]) == 1
    assert classees["new"][0]["label"] == "HKCU\\Run · Updater"
    assert "identity" not in classees["new"][0]


def test_a_corrupted_state_does_not_break_the_scan(tmp_path: Path) -> None:
    """Un état illisible produit un rapport bruyant une fois, pas une routine
    cassée jusqu'à intervention manuelle."""
    chemin = tmp_path / "state.json"
    chemin.write_text("{ ceci n'est pas du json", encoding="utf-8")

    etat = sentinel.load_state(chemin)

    assert etat["baseline"] == {}
    assert etat["acknowledged"] == {}


def test_state_survives_a_round_trip(tmp_path: Path) -> None:
    chemin = tmp_path / "sentinel" / "state.json"
    sentinel.save_state(chemin, {"baseline": {"abc": "2026-01-01"}, "acknowledged": {}})

    assert json.loads(chemin.read_text(encoding="utf-8"))["baseline"] == {"abc": "2026-01-01"}
    assert sentinel.load_state(chemin)["baseline"] == {"abc": "2026-01-01"}


def test_a_failing_section_does_not_cancel_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Une sonde qui casse doit coûter sa section, pas le rapport entier."""

    def explose():
        raise OSError("sonde indisponible")

    monkeypatch.setitem(sentinel.COLLECTORS, "connections", explose)

    rapports = {rapport.name: rapport for rapport in sentinel.scan_sections(sentinel.SECTIONS)}

    assert rapports["connections"].findings == []
    assert rapports["connections"].limitations == ["section interrompue : OSError"]
    assert "input_hooks" in rapports


def test_every_platform_declares_its_keylogger_blind_spot() -> None:
    """Se taire sur un angle mort laisserait croire à une couverture absente.

    Aucune plateforme n'est couverte nativement : macOS demande ReiKey, et
    Windows n'expose tout simplement pas ses hooks clavier en espace
    utilisateur.
    """
    assert sentinel.collect_input_hooks().limitations


def _context(tmp_path: Path, orchestrateur: str = "main"):
    events = SimpleNamespace(directory=tmp_path / "content-agents" / "sessions")
    return SimpleNamespace(
        deps=SimpleNamespace(
            events=events,
            state_db=tmp_path / "content-agents" / "state.db",
            orchestrator_id=orchestrateur,
            workspace=tmp_path,
        )
    )


async def test_the_state_belongs_to_the_orchestrator(tmp_path: Path) -> None:
    """Même règle que la bibliothèque : un sous-agent alimente la référence de
    l'orchestrateur, sinon son acquittement disparaîtrait avec la délégation."""
    chemin = sentinel._state_path(_context(tmp_path, "sophie"))

    assert chemin.parent.parent.name == "sophie"
    assert chemin.parent.name == "sentinel"


async def test_an_unknown_orchestrator_falls_back_to_a_shared_state(tmp_path: Path) -> None:
    chemin = sentinel._state_path(_context(tmp_path, "../evasion"))

    assert chemin == tmp_path / "content-agents" / "sentinel" / "state.json"


async def test_the_first_scan_announces_itself_as_a_baseline(tmp_path: Path) -> None:
    """Le premier passage n'a rien à quoi se comparer : tout y est inédit sans
    que rien ne soit suspect. Le taire donnerait un rapport alarmant le premier
    jour puis muet les suivants."""
    contexte = _context(tmp_path)

    premier = await sentinel.sentinel_scan(contexte)
    second = await sentinel.sentinel_scan(contexte)

    assert premier["data"]["baseline_established"] is True
    assert second["data"]["baseline_established"] is False
    assert second["data"]["new"] == []


async def test_acknowledging_silences_a_finding_on_the_next_scan(tmp_path: Path) -> None:
    contexte = _context(tmp_path)
    observation = _finding()
    chemin = sentinel._state_path(contexte)
    sentinel.save_state(chemin, {"baseline": {}, "acknowledged": {}})

    await sentinel.sentinel_acknowledge(contexte, [observation.fingerprint], note="connu")

    etat = sentinel.load_state(chemin)
    assert sentinel.classify([observation], etat)["new"] == []
    assert etat["acknowledged"][observation.fingerprint]["note"] == "connu"


async def test_the_scan_reports_what_it_could_not_cover(tmp_path: Path) -> None:
    rapport = await sentinel.sentinel_scan(_context(tmp_path))

    sections = {item["section"] for item in rapport["data"]["limitations"]}
    assert "input_hooks" in sections


def test_private_and_loopback_traffic_is_ignored() -> None:
    """Le trafic interne n'apprend rien et noierait les sorties réelles."""
    assert sentinel._is_private("127.0.0.1")
    assert sentinel._is_private("192.168.1.20")
    assert not sentinel._is_private("8.8.8.8")


def test_a_malformed_address_is_treated_as_private() -> None:
    """Face à l'illisible, se taire plutôt que d'alerter à tort."""
    assert sentinel._is_private("pas-une-adresse")


def test_system_binaries_are_left_out(tmp_path: Path) -> None:
    racines = (tmp_path / "systeme",)

    assert sentinel._is_system_path(tmp_path / "systeme" / "svc.exe", racines)
    assert not sentinel._is_system_path(tmp_path / "utilisateur" / "svc.exe", racines)


def test_declared_mcp_servers_are_read_from_both_shapes(tmp_path: Path) -> None:
    """Les serveurs MCP se déclarent au niveau racine ou par projet ; en manquer
    une forme reviendrait à ne pas voir le serveur qui compte."""
    document = tmp_path / ".claude.json"
    document.write_text(
        json.dumps(
            {
                "mcpServers": {"global": {}},
                "projects": {"/un/projet": {"mcpServers": {"local": {}}}},
            }
        ),
        encoding="utf-8",
    )

    assert sorted(sentinel._declared_mcp_servers(document)) == ["global", "local"]


def test_an_unreadable_configuration_yields_nothing(tmp_path: Path) -> None:
    document = tmp_path / "casse.json"
    document.write_text("{", encoding="utf-8")

    assert list(sentinel._declared_mcp_servers(document)) == []


async def test_a_scan_can_leave_the_baseline_untouched(tmp_path: Path) -> None:
    """Rejouer un scan sans absorber ses résultats permet de re-lire un écart
    sans l'avoir accepté au passage."""
    contexte = _context(tmp_path)
    await sentinel.sentinel_scan(contexte)
    chemin = sentinel._state_path(contexte)
    avant = sentinel.load_state(chemin)

    sentinel.save_state(chemin, {**avant, "baseline": {}})
    rapport = await sentinel.sentinel_scan(contexte, update_baseline=False)

    assert sentinel.load_state(chemin)["baseline"] == {}
    assert rapport["data"]["baseline_established"] is True


async def test_an_unknown_section_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    rapport = await sentinel.sentinel_scan(_context(tmp_path), sections=["inexistante"])

    assert rapport["data"]["sections"] == list(sentinel.SECTIONS)


def test_reikey_findings_replace_the_macos_blind_spot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ReiKey est le seul outil qui change la nature du résultat, pas son volume :
    sans lui la section n'a rien à dire, avec lui elle conclut."""
    binaire = tmp_path / "ReiKey"
    binaire.write_text("", encoding="utf-8")
    monkeypatch.setattr(sentinel.sys, "platform", "darwin")
    monkeypatch.setattr(sentinel, "REIKEY_BINARY", binaire)
    monkeypatch.setattr(
        sentinel,
        "_run_probe",
        lambda command: json.dumps({"event taps": [{"process path": "/tmp/espion"}]}),
    )

    rapport = sentinel.enrich_input_hooks(
        sentinel.SectionReport("input_hooks", limitations=["… installer ReiKey"])
    )

    assert rapport.limitations == []
    assert rapport.findings[0].severity == "warning"
    assert rapport.findings[0].label == "Event tap · espion"


def test_a_silent_reikey_leaves_the_limitation_standing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binaire = tmp_path / "ReiKey"
    binaire.write_text("", encoding="utf-8")
    monkeypatch.setattr(sentinel.sys, "platform", "darwin")
    monkeypatch.setattr(sentinel, "REIKEY_BINARY", binaire)
    monkeypatch.setattr(sentinel, "_run_probe", lambda command: None)

    rapport = sentinel.enrich_input_hooks(sentinel.SectionReport("input_hooks"))

    assert rapport.limitations == ["ReiKey présent mais son scan n'a pas abouti"]


def test_osquery_covers_the_windows_anchors_the_kernel_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tâches planifiées et services sont des ancrages aussi utilisés que les
    clés Run, et `winreg` n'y donne pas accès."""
    monkeypatch.setattr(sentinel.sys, "platform", "win32")
    monkeypatch.setattr(
        sentinel,
        "_osquery_rows",
        lambda query: (
            [{"name": "MiseAJour", "action": "C:/t.exe"}]
            if "scheduled_tasks" in query
            else [{"name": "Svc", "path": "C:/s.exe"}]
        ),
    )
    manquant = "tâches planifiées et services non couverts par le socle natif"

    rapport = sentinel.enrich_persistence(
        sentinel.SectionReport("persistence", limitations=[manquant])
    )

    assert rapport.limitations == []
    assert {item.kind for item in rapport.findings} == {"scheduled_task", "service"}


def test_without_osquery_the_gap_is_named_with_its_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sentinel.sys, "platform", "win32")
    monkeypatch.setattr(sentinel, "_osquery_rows", lambda query: None)

    rapport = sentinel.enrich_persistence(sentinel.SectionReport("persistence"))

    assert "installer osquery" in rapport.limitations[0]


def test_dev_machine_guard_is_announced_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deviner son schéma produirait des écarts faux ou muets — pire que rien."""
    monkeypatch.setattr(sentinel.shutil, "which", lambda name: "/usr/local/bin/dmg")

    rapport = sentinel.enrich_ai_environment(sentinel.SectionReport("ai_environment"))

    assert rapport.findings == []
    assert "n'est pas encore interprétée" in rapport.limitations[0]
