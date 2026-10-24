"""Layer 1: the places things run. Each implements execution.Runtime and documents its own guarantees."""
from .docker import ContainerExecution, DockerRuntime, Resources, owner_of, signal

__all__ = ["ContainerExecution", "DockerRuntime", "Resources", "owner_of", "signal"]
