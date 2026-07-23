class KernelError(Exception):
    """Base error for expected kernel failures."""


class ConfigurationError(KernelError):
    """Configuration is invalid and must not be retried."""


class AuthenticationError(KernelError):
    """Credentials are missing or invalid and must not be retried."""


class ModuleError(KernelError):
    """A module cannot be loaded or validated."""


class BudgetExceeded(KernelError):
    """A kernel-level orchestration budget was exhausted."""

