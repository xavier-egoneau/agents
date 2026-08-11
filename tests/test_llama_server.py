from pathlib import Path

import pytest

from agentic_kernel.llama_server import (
    LlamaServerError,
    LlamaServerManager,
    ThroughputCounters,
    parse_throughput_counters,
    resolve_gguf_path,
    scan_gguf_models,
)


def test_scan_collapses_gguf_shards_and_is_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "plain.GGUF").write_bytes(b"")
    (tmp_path / "sharded-00001-of-00002.gguf").write_bytes(b"")
    (tmp_path / "sharded-00002-of-00002.gguf").write_bytes(b"")

    assert scan_gguf_models(tmp_path) == ["plain", "sharded"]
    assert resolve_gguf_path(tmp_path, "sharded").name == "sharded-00001-of-00002.gguf"


def test_missing_model_lists_available_models(tmp_path: Path) -> None:
    (tmp_path / "available.gguf").write_bytes(b"")

    with pytest.raises(LlamaServerError, match="available"):
        resolve_gguf_path(tmp_path, "missing")


def test_manager_builds_loopback_only_server_command(tmp_path: Path) -> None:
    binary = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    binary.write_bytes(b"")
    model.write_bytes(b"")
    manager = LlamaServerManager(
        state_dir=tmp_path / "state",
        models_dir=tmp_path,
        provider_id="local",
        binary=str(binary),
        port=8123,
        server_args=["--ctx-size", "16384", "--flash-attn", "on"],
    )

    argv = manager._build_argv(manager.resolve_binary(), model)

    assert argv[:8] == [
        str(binary.resolve()),
        "-m",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
        "--jinja",
    ]
    assert argv[-4:] == ["--ctx-size", "16384", "--flash-attn", "on"]


def test_manager_rejects_network_and_model_overrides(tmp_path: Path) -> None:
    binary = tmp_path / "llama-server.exe"
    (tmp_path / "model.gguf").write_bytes(b"")
    binary.write_bytes(b"")
    manager = LlamaServerManager(
        state_dir=tmp_path / "state",
        models_dir=tmp_path,
        provider_id="local",
        binary=str(binary),
        server_args=["--host=0.0.0.0"],
    )

    with pytest.raises(LlamaServerError, match="administré par AMK"):
        manager.validate_configuration()


def test_throughput_is_read_from_the_difference_between_two_readings() -> None:
    """Le débit d'un run se lit entre deux relevés, pas dans une moyenne globale.

    `/metrics` publie des cumuls depuis le démarrage du serveur. Les lire tels
    quels donnerait la moyenne de toute la session, y compris des runs
    antérieurs faits dans d'autres conditions. La soustraction isole le travail
    de ce run, et reste juste quand il a enchaîné plusieurs requêtes.
    """
    corps = """# HELP llamacpp:prompt_tokens_total Prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 40000
llamacpp:prompt_seconds_total 20
llamacpp:tokens_predicted_total 1500
llamacpp:tokens_predicted_seconds_total 15
"""
    avant = ThroughputCounters(
        prompt_tokens=21000, prompt_seconds=10, predicted_tokens=557, predicted_seconds=10
    )

    apres = parse_throughput_counters(corps)

    assert apres is not None
    assert apres.since(avant) == {
        "prefill_tokens_per_second": 1900.0,
        "generation_tokens_per_second": 188.6,
    }


def test_an_incomplete_metrics_body_yields_no_measurement() -> None:
    """Un débit calculé sur des compteurs partiels tromperait plus qu'il n'informe."""
    assert parse_throughput_counters("llamacpp:prompt_tokens_total 40000\n") is None


def test_a_reading_without_elapsed_time_yields_no_rate() -> None:
    """Deux relevés identiques ne prouvent aucune vitesse : on n'en publie pas."""
    releve = ThroughputCounters(
        prompt_tokens=10, prompt_seconds=1, predicted_tokens=10, predicted_seconds=1
    )

    assert releve.since(releve) == {}
