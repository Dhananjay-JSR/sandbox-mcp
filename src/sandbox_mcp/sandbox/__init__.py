"""Sandbox backends. Docker today; the interface is what other runtimes target."""

from .interface import ExecOutcome, SandboxBackend, SandboxHandle

__all__ = ["ExecOutcome", "SandboxBackend", "SandboxHandle"]
