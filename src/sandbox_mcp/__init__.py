"""Sandbox MCP -- disposable, isolated execution environments for AI coding agents.

The developer's machine stays untouched; the agent gets somewhere real to work.
"""

from .app import SandboxMCPApp
from .config import Settings, get_settings
from .errors import SandboxMCPError
from .models import (
    ChangeSet,
    Experiment,
    ExperimentStatus,
    Job,
    JobStatus,
    MountStrategy,
    NetworkMode,
)
from .server import create_server

__version__ = "0.1.0"

__all__ = [
    "ChangeSet",
    "Experiment",
    "ExperimentStatus",
    "Job",
    "JobStatus",
    "MountStrategy",
    "NetworkMode",
    "SandboxMCPApp",
    "SandboxMCPError",
    "Settings",
    "__version__",
    "create_server",
    "get_settings",
]
