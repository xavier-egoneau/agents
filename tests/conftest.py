from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aucun test ne doit pouvoir atteindre les données réelles.

    `content_root()` consulte un réglage global enregistré hors du dépôt, et cet
    emplacement l'emporte sur la racine qu'on lui passe. Un test construisant un
    `ProjectConfig` sur un dossier temporaire écrivait donc dans le
    `content-agents` réel de l'utilisateur : 96 routines de test s'y sont
    accumulées avant qu'on s'en aperçoive, dont 72 activées.

    `AMK_HOME` est le mécanisme prévu pour « un lancement ponctuel sur un autre
    jeu de données ». Le harnais ne s'en servait pas ; il s'en sert maintenant,
    pour tous les tests, sans qu'aucun ait à y penser.
    """
    monkeypatch.setenv("AMK_HOME", str(tmp_path))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    content = tmp_path / "content-agents"
    agents = content / "agents"
    modules = tmp_path / "tools" / "modules" / "clock"
    agents.mkdir(parents=True)
    modules.mkdir(parents=True)
    (content / "system.md").write_text("System instructions.", encoding="utf-8")
    (content / "providers.json").write_text(
        json.dumps(
            {
                "version": 1,
                "default_provider": "test",
                "providers": [
                    {
                        "id": "test",
                        "kind": "llama-cpp",
                        "connection_type": "local",
                        "base_url": "http://localhost:9999",
                        "model": "test-model",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (agents / "main.md").write_text(
        """---
id: main
description: Main test agent
provider: test
modules: [clock]
delegates: []
---
Answer the request.
""",
        encoding="utf-8",
    )
    (modules / "module.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "clock",
                "name": "Clock",
                "description": "Test clock",
                "version": "1.0.0",
                "entrypoint": "module.py:module",
                "capabilities": ["tools"],
                "enabled": True,
            }
        ),
        encoding="utf-8",
    )
    (modules / "module.py").write_text(
        """class Module:
    def toolsets(self): return []
    def instructions(self): return []
    def capabilities(self): return []
module = Module()
""",
        encoding="utf-8",
    )
    return tmp_path
