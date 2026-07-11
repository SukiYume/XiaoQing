import builtins
import os

import main as entrypoint


def test_runtime_preparation_does_not_override_openmp_policy(monkeypatch) -> None:
    original_import = builtins.__import__

    def without_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("optional dependency unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_torch)
    monkeypatch.setenv("KMP_DUPLICATE_LIB_OK", "operator-chosen-value")

    entrypoint._prepare_runtime()

    assert os.environ["KMP_DUPLICATE_LIB_OK"] == "operator-chosen-value"


def test_runtime_preparation_does_not_create_openmp_workaround(monkeypatch) -> None:
    monkeypatch.delenv("KMP_DUPLICATE_LIB_OK", raising=False)
    entrypoint._prepare_runtime()

    assert "KMP_DUPLICATE_LIB_OK" not in os.environ
