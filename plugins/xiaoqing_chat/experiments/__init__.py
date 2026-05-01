"""Experiment utilities for xiaoqing_chat."""

__all__ = [
    "ExperimentConfig",
    "GeneratedTurn",
    "Persona",
    "generate_matrix",
    "score_turn",
    "write_experiment_artifacts",
]


def __getattr__(name):
    if name in __all__:
        from . import anthropomorphic_group

        return getattr(anthropomorphic_group, name)
    raise AttributeError(name)
