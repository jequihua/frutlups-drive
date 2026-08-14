"""frutlups-drive: execution runtime for artifact-first agentic coding loops.

The package export policy is deliberately tiny: the top-level module exposes
the version only. Every component is imported explicitly from its submodule
(for example ``from frutlups_drive.contracts import PlanOutcome``).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
