from __future__ import annotations

from pathlib import Path

from agentic_kernel.secrets import REDACTION_MIN_LENGTH, SecretStore


def test_short_secrets_are_not_redacted_everywhere(tmp_path: Path) -> None:
    """« x » ou « test » se substituerait dans tous les textes ordinaires.

    La redaction par valeur sans longueur minimale corrompait les sorties
    d'outils dès qu'un secret court était enregistré.
    """
    store = SecretStore(tmp_path / "secrets.json")
    store.set("court", "x")
    store.set("autre", "test")

    text = "exit code 0; test de la sortie; x est une variable"
    assert store.redact(text) == text


def test_long_secrets_are_redacted_by_value(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    store.set("jeton", "sk-" + "a" * 40)

    text = f"autorisation: sk-{'a' * 40} — fin"
    redacted = store.redact(text)

    assert redacted == "autorisation: *** — fin"
    assert len("sk-" + "a" * 40) >= REDACTION_MIN_LENGTH


def test_redaction_keeps_masking_inside_nested_structures(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "secrets.json")
    store.set("jeton", "super-secret-value")

    value = {"outer": ["usage de super-secret-value ici", {"inner": "super-secret-value"}]}
    redacted = store.redact(value)

    assert redacted == {"outer": ["usage de *** ici", {"inner": "***"}]}
