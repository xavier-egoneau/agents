"""Cross-platform operating-system services used by the kernel."""

from .sandbox import SandboxCapabilities, sandbox_capabilities

__all__ = ["SandboxCapabilities", "sandbox_capabilities"]
