from __future__ import annotations

from typing import Any

from .platform.sandbox import PreparedExecution, prepare_execution, sandbox_capabilities


class ExecutionSandbox:
    """Compatibility facade over the selected operating-system sandbox backend."""

    @staticmethod
    def available() -> bool:
        return sandbox_capabilities().execution_isolated

    @staticmethod
    def capabilities():
        return sandbox_capabilities()

    @staticmethod
    def prepare(
        command: list[str],
        deps: Any,
        *,
        allow_network: bool = False,
        publish_ports: list[int] | None = None,
    ) -> PreparedExecution:
        return prepare_execution(
            command, deps, allow_network=allow_network, publish_ports=publish_ports
        )


__all__ = ["ExecutionSandbox", "PreparedExecution"]
