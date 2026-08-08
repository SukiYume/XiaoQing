from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from plugins.arxiv_filter.inference.shared import InferenceParams, build_paper_texts
from plugins.arxiv_filter.numerics import stable_softmax
from plugins.arxiv_filter.train_model.interest_model import training_utils as interest_utils
from tests.helpers.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def _load_data_prep_module(name: str, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """数据准备脚本把 feedparser 作为可选离线依赖，测试只注入最小替身。"""

    monkeypatch.setitem(sys.modules, "feedparser", SimpleNamespace())
    return importlib.import_module(f"plugins.arxiv_filter.train_model.data_prep.{name}")


def _load_knn_training_module() -> ModuleType:
    """仅在本地 ML 依赖完整时加载 KNN 训练入口。"""

    pytest.importorskip("torch")
    return importlib.import_module("plugins.arxiv_filter.train_model.interest_model.knn_arxiv")


def test_arxiv_runtime_keeps_only_the_used_fetch_and_knn_scoring_entrypoints() -> None:
    fetch_tree = ast.parse(
        (ROOT / "plugins" / "arxiv_filter" / "arxiv_today.py").read_text(encoding="utf-8")
    )
    fetch_functions = {
        node.name
        for node in fetch_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "get_today_arxiv" in fetch_functions
    assert "get_today_arxiv_api" not in fetch_functions

    knn_tree = ast.parse(
        (
            ROOT / "plugins" / "arxiv_filter" / "train_model" / "interest_model" / "knn_arxiv.py"
        ).read_text(encoding="utf-8")
    )
    knn_class = next(
        node
        for node in knn_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KNNInterestModel"
    )
    method_names = {
        node.name
        for node in knn_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "predict_proba" in method_names
    assert "predict" not in method_names


@pytest.fixture
def inference_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SimpleNamespace]:
    """Import pure inference helpers without making torch a test dependency."""
    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    fake_torch.device = lambda name: name  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    package = importlib.import_module("plugins.arxiv_filter.inference")
    module_names = (
        "plugins.arxiv_filter.inference.knn_backend",
        "plugins.arxiv_filter.inference.multi_interest_backend",
        "plugins.arxiv_filter.inference.runner",
    )
    attribute_names = ("knn_backend", "multi_interest_backend", "runner")
    previous_modules = {name: sys.modules.get(name) for name in module_names}
    previous_attributes = {
        name: getattr(package, name) for name in attribute_names if hasattr(package, name)
    }

    imported = [importlib.import_module(name) for name in module_names]
    try:
        yield SimpleNamespace(
            knn_backend=imported[0],
            multi_interest_backend=imported[1],
            runner=imported[2],
        )
    finally:
        for name, previous in previous_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        for name in attribute_names:
            if name in previous_attributes:
                setattr(package, name, previous_attributes[name])
            else:
                vars(package).pop(name, None)


def test_bert_loader_shuffles_training_but_not_validation() -> None:
    torch = pytest.importorskip("torch")
    training_utils = importlib.import_module(
        "plugins.arxiv_filter.train_model.bert_model.training_utils"
    )
    dataset = list(range(6))

    training = training_utils.create_seeded_data_loader(
        dataset,
        collate_fn=list,
        batch_size=2,
        random_seed=17,
    )
    validation = training_utils.create_seeded_data_loader(
        dataset,
        collate_fn=list,
        batch_size=2,
        random_seed=17,
        shuffle=False,
    )

    assert isinstance(training.sampler, torch.utils.data.RandomSampler)
    assert isinstance(validation.sampler, torch.utils.data.SequentialSampler)


def test_bert_training_bootstrap_builds_shared_resources_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    training_utils = importlib.import_module(
        "plugins.arxiv_filter.train_model.bert_model.training_utils"
    )
    frame = pd.DataFrame({"title": ["a", "b", "c", "d"], "label": [0, 1, 0, 1]})
    train_frame = frame.iloc[:2].copy()
    validation_frame = frame.iloc[2:].copy()
    logs: list[str] = []
    loader_calls: list[dict[str, object]] = []
    sampler = object()
    optimizer = object()

    monkeypatch.setattr(training_utils, "seed_everything", lambda _seed: None)
    monkeypatch.setattr(training_utils, "timestamp_log", logs.append)
    monkeypatch.setattr(training_utils, "read_training_csv", lambda _path: frame.copy())
    monkeypatch.setattr(
        training_utils,
        "split_train_validation_frame",
        lambda *_args, **_kwargs: (train_frame, validation_frame),
    )
    monkeypatch.setattr(
        training_utils,
        "compute_class_weight",
        lambda **_kwargs: np.array([1.0, 1.0]),
    )
    monkeypatch.setattr(training_utils, "build_weighted_sampler", lambda *_args: sampler)
    monkeypatch.setattr(
        training_utils,
        "get_runtime_settings",
        lambda _device: {
            "use_amp": False,
            "pin_memory": False,
            "use_fused": False,
            "amp_dtype": torch.float32,
        },
    )
    monkeypatch.setattr(training_utils, "create_optimizer", lambda *_args, **_kwargs: optimizer)

    class TokenizerFactory:
        @staticmethod
        def from_pretrained(model_name: str) -> object:
            assert model_name == "model/demo"
            return object()

    class Model:
        def __init__(self) -> None:
            self.device = None

        def to(self, device: object) -> None:
            self.device = device

        @staticmethod
        def parameters() -> list[object]:
            return []

    model = Model()

    class ModelFactory:
        @staticmethod
        def from_pretrained(model_name: str, *, num_labels: int) -> Model:
            assert (model_name, num_labels) == ("model/demo", 2)
            return model

    def create_loader(*_args: object, **kwargs: object) -> list[object]:
        loader_calls.append(kwargs)
        return [object(), object()]

    scheduler_calls: list[dict[str, int]] = []

    def create_scheduler(_optimizer: object, **kwargs: int) -> object:
        scheduler_calls.append(kwargs)
        return object()

    config = SimpleNamespace(
        random_seed=17,
        data_path=tmp_path / "training.csv",
        validation_size=0.5,
        model_name="model/demo",
        max_len=64,
        batch_size=2,
        num_workers=0,
        learning_rate=2e-5,
        num_epochs=3,
        warmup_proportion=0.5,
        output_dir=tmp_path / "model",
    )

    runtime = training_utils.prepare_classifier_training(
        config,
        device=torch.device("cpu"),
        classifier_name="test classifier",
        prepare_frame=lambda value: value,
        create_loader=create_loader,
        tokenizer_factory=TokenizerFactory,
        model_factory=ModelFactory,
        scheduler_factory=create_scheduler,
    )

    assert runtime.frame.equals(frame)
    assert runtime.train_frame.equals(train_frame)
    assert runtime.validation_frame.equals(validation_frame)
    assert runtime.optimizer is optimizer
    assert model.device == torch.device("cpu")
    assert loader_calls[0]["sampler"] is sampler
    assert loader_calls[1]["shuffle"] is False
    assert scheduler_calls == [{"num_warmup_steps": 3, "num_training_steps": 6}]
    assert config.output_dir.is_dir()
    assert any("test classifier" in line for line in logs)


def test_data_prep_month_ranges_preserve_inclusive_day_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step2 = _load_data_prep_module("step2_fetch_all_astro_ph", monkeypatch)

    assert step2.generate_monthly_ranges("2026-07-15", "2026-08-06") == [
        ("202607150000", "202607312359", 2607),
        ("202608010000", "202608062359", 2608),
    ]
    assert step2.generate_monthly_ranges("2026-08-06", "2026-08-06") == [
        ("202608060000", "202608062359", 2608)
    ]
    with pytest.raises(ValueError, match="start must not be after end"):
        step2.generate_monthly_ranges("2026-08-07", "2026-08-06")


def test_data_prep_rejects_invalid_dataset_before_overwriting_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    step3 = _load_data_prep_module("step3_build_dataset", monkeypatch)
    output_path = tmp_path / "training.csv"
    output_path.write_text("existing\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="为空"):
        step3._write_final_dataset(
            pd.DataFrame(columns=["arXiv ID", "Title", "Abstract", "label"]),
            output_path,
        )
    with pytest.raises(ValueError, match="缺少列"):
        step3._write_final_dataset(pd.DataFrame({"arXiv ID": ["2608.00001"]}), output_path)

    assert output_path.read_text(encoding="utf-8") == "existing\n"


def test_knn_config_follows_custom_output_directory(tmp_path: Path) -> None:
    module = _load_knn_training_module()
    output_dir = tmp_path / "model"

    default_cache = module.KNNConfig(output_dir=output_dir)
    explicit_cache = module.KNNConfig(
        output_dir=output_dir,
        emb_cache_dir=tmp_path / "shared-cache",
    )

    assert default_cache.resolved_emb_cache_dir == output_dir / "emb_cache"
    assert explicit_cache.resolved_emb_cache_dir == tmp_path / "shared-cache"


def test_knn_training_reuses_known_embedding_dimension_and_saves_max_len(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_knn_training_module()
    load_calls = 0

    def load_encoder() -> object:
        nonlocal load_calls
        load_calls += 1
        raise AssertionError("precomputed training must not load the encoder")

    monkeypatch.setattr(module._training, "load_sentence_transformer_class", load_encoder)
    model = module.KNNInterestModel(
        embedding_dim=2,
        max_len=128,
        neg_sample_size=2,
    )
    frame = pd.DataFrame(
        {
            "Title": ["positive", "negative"],
            "Abstract": ["wanted", "ignored"],
            "label": [1, 0],
        }
    )
    model.fit(
        frame,
        precomputed_embeddings=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    model.save(tmp_path)

    config = (tmp_path / "training_config.json").read_text(encoding="utf-8")
    assert load_calls == 0
    assert '"max_len": 128' in config


def test_stable_softmax_is_finite_normalized_and_shift_invariant() -> None:
    values = np.array([[1000.0, 1001.0], [-1000.0, -999.0]])

    probabilities = stable_softmax(values)
    shifted = stable_softmax(values + 10_000.0)

    assert np.isfinite(probabilities).all()
    assert np.allclose(probabilities.sum(axis=1), np.ones(2))
    assert np.allclose(probabilities, shifted)


def test_knn_backend_builds_normalized_text_and_applies_negative_penalty(
    inference_modules: SimpleNamespace,
) -> None:
    knn_backend = inference_modules.knn_backend
    frame = pd.DataFrame(
        {
            "Title": ["  First\n title ", None],
            "Abstract": [" abstract\tvalue ", "second"],
        }
    )
    assert build_paper_texts(frame, "Title", "Abstract") == [
        "Title: First title\nAbstract: abstract value",
        "Title: \nAbstract: second",
    ]

    model = object.__new__(knn_backend.KNNInferenceModel)
    model.pos_embeddings = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    model.neg_embeddings = np.array([[-1.0, 0.0]], dtype=np.float32)
    model.k = 1
    model.neg_k = 1
    model.neg_weight = 0.5

    scores = model._score(np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))

    assert np.allclose(scores, np.array([1.5, 1.0], dtype=np.float32))


def test_knn_backend_rejects_an_empty_interest_library(
    inference_modules: SimpleNamespace,
    tmp_path: Path,
) -> None:
    knn_backend = inference_modules.knn_backend
    (tmp_path / "meta.json").write_text(
        '{"encoder_name":"encoder","embed_dim":2,"k":1,"columns":{}}',
        encoding="utf-8",
    )
    np.save(tmp_path / "pos_embeddings.npy", np.empty((0, 2), dtype=np.float32))

    with pytest.raises(ValueError, match="at least one row"):
        knn_backend.KNNInferenceModel(str(tmp_path), batch_size=1)


def test_multi_interest_feature_builder_is_finite_and_preserves_contrast(
    inference_modules: SimpleNamespace,
) -> None:
    multi_interest_backend = inference_modules.multi_interest_backend
    model = object.__new__(multi_interest_backend.MultiInterestInferenceModel)
    model.artifacts = SimpleNamespace(
        interest_centers=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        pos_centroid=np.array([1.0, 0.0], dtype=np.float32),
        neg_centroid=np.array([0.0, 1.0], dtype=np.float32),
    )

    features = model._build_features(np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))

    assert features.shape == (2, 12)
    assert np.isfinite(features).all()
    assert np.allclose(features[:, -1], np.array([1.0, -1.0], dtype=np.float32))


def test_inference_runner_copies_input_and_publishes_backend_results(
    monkeypatch: pytest.MonkeyPatch,
    inference_modules: SimpleNamespace,
) -> None:
    runner = inference_modules.runner
    params = InferenceParams(
        model_path="model",
        threshold=0.6,
        batch_size=2,
        max_len=32,
        model_type="knn",
    )
    monkeypatch.setattr(runner, "resolve_params", lambda *_args, **_kwargs: params)
    monkeypatch.setattr(runner, "_dispatch_inference", lambda *_args: ([0.8, 0.2], [1, 0]))
    source = pd.DataFrame({"Title": ["first", "second"]})

    output, threshold = runner.run_inference_for_dataframe(source)

    assert output is not None
    assert output is not source
    assert "Probability" not in source.columns
    assert output["Probability"].tolist() == [0.8, 0.2]
    assert output["Prediction"].tolist() == [1, 0]
    assert threshold == 0.6
    assert runner.select_positives(output)["Title"].tolist() == ["first"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_path", ""),
        ("threshold", float("nan")),
        ("batch_size", True),
        ("batch_size", 0),
        ("max_len", -1),
        ("input_mode", "unknown"),
        ("model_type", "unknown"),
    ],
)
def test_inference_params_reject_invalid_runtime_values(field: str, value: object) -> None:
    values = {
        "model_path": "model",
        "threshold": 0.5,
        "batch_size": 2,
        "max_len": 32,
        "input_mode": "title_abstract",
        "model_type": "knn",
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        InferenceParams(**values)


@pytest.mark.parametrize(
    "backend_result",
    [
        ([0.5], [1]),
        ([0.5, float("nan")], [1, 0]),
        ([0.5, 0.4], [1, True]),
    ],
)
def test_inference_runner_rejects_malformed_backend_results(
    monkeypatch: pytest.MonkeyPatch,
    inference_modules: SimpleNamespace,
    backend_result: tuple[list[float], list[int]],
) -> None:
    params = InferenceParams("model", 0.5, 2, 32, model_type="knn")
    data = pd.DataFrame({"Title": ["first", "second"]})
    monkeypatch.setattr(
        inference_modules.knn_backend,
        "run_knn_inference",
        lambda *_args: backend_result,
    )

    with pytest.raises(ValueError, match="inference backend returned"):
        inference_modules.runner._dispatch_inference(params, data)


def test_interest_column_resolution_preserves_alias_order_and_required_errors() -> None:
    frame = pd.DataFrame(columns=["Paper Title", "Summary", "Target", "Article ID"])
    aliases = {
        "id": ["arxivid", "articleid"],
        "title": ["title", "papertitle"],
        "abstract": ["abstract", "summary"],
        "label": ["label", "target"],
    }

    assert interest_utils.resolve_columns(
        frame,
        aliases,
        required_fields={"title", "abstract"},
        error_prefix="missing columns",
    ) == {
        "id": "Article ID",
        "title": "Paper Title",
        "abstract": "Summary",
        "label": "Target",
    }

    with pytest.raises(ValueError, match=r"missing columns.*候选：\['abstract', 'summary'\]"):
        interest_utils.resolve_columns(
            frame.drop(columns=["Summary"]),
            aliases,
            required_fields={"abstract"},
            error_prefix="missing columns",
        )


def test_interest_split_supports_deterministic_random_and_time_policies() -> None:
    frame = pd.DataFrame(
        {
            "id": list(range(8)),
            "label": [0, 1] * 4,
            "date": [f"2030-01-{day:02d}" for day in range(8, 0, -1)],
        }
    )
    common = {
        "missing_date_error": "date required",
        "stratify_fallback_message": "fallback",
    }

    train_a, validation_a = interest_utils.split_dataframe(
        frame,
        0.25,
        "random",
        seed=17,
        label_col="label",
        **common,
    )
    train_b, validation_b = interest_utils.split_dataframe(
        frame,
        0.25,
        "random",
        seed=17,
        label_col="label",
        **common,
    )
    assert train_a.equals(train_b)
    assert validation_a.equals(validation_b)
    assert sorted(validation_a["label"].tolist()) == [0, 1]

    time_train, time_validation = interest_utils.split_dataframe(
        frame,
        0.25,
        "time",
        date_col="date",
        **common,
    )
    assert time_train["date"].tolist() == [
        "2030-01-01",
        "2030-01-02",
        "2030-01-03",
        "2030-01-04",
        "2030-01-05",
        "2030-01-06",
    ]
    assert time_validation["date"].tolist() == ["2030-01-07", "2030-01-08"]

    with pytest.raises(ValueError, match="unknown: other"):
        interest_utils.split_dataframe(
            frame,
            0.25,
            "other",
            unknown_mode_error="unknown: {mode}",
            **common,
        )


def test_interest_metrics_cover_single_class_and_ranked_precision() -> None:
    assert interest_utils.best_fbeta_threshold(
        np.array([1, 1]),
        np.array([0.2, 0.8]),
    ) == (0.5, 0.0)

    threshold, score = interest_utils.best_fbeta_threshold(
        np.array([0, 0, 1, 1]),
        np.array([0.1, 0.4, 0.8, 0.9]),
        beta=2.0,
    )
    assert 0.0 <= threshold <= 1.0
    assert score == pytest.approx(1.0)
    assert (
        interest_utils.precision_at_k(
            np.array([0, 1, 1]),
            np.array([0.1, 0.9, 0.8]),
            2,
        )
        == 1.0
    )
    assert interest_utils.precision_at_k(np.array([]), np.array([]), 0) == 0.0


@pytest.mark.parametrize("labels", [[0, 2], [True, False], [0, float("nan")], []])
def test_training_label_normalization_rejects_non_binary_values(labels: list[object]) -> None:
    with pytest.raises(ValueError, match="only 0 and 1"):
        interest_utils.coerce_binary_labels(labels)


def test_embedding_validation_rejects_wrong_rows_dimensions_and_nan() -> None:
    with pytest.raises(ValueError, match="2 rows"):
        interest_utils.validate_embedding_matrix(np.ones((1, 2)), expected_rows=2)
    with pytest.raises(ValueError, match="dimension 3"):
        interest_utils.validate_embedding_matrix(
            np.ones((2, 2)),
            expected_rows=2,
            expected_dim=3,
        )
    with pytest.raises(ValueError, match="finite"):
        interest_utils.validate_embedding_matrix(
            np.array([[0.0, float("nan")]]),
            expected_rows=1,
        )


def test_training_csv_reader_preserves_arxiv_id_trailing_zero(tmp_path: Path) -> None:
    path = tmp_path / "training.csv"
    path.write_text(
        "arXiv ID,Title,Abstract,label\n2410.03200,title,abstract,1\n",
        encoding="utf-8",
    )

    frame = interest_utils.read_training_csv(path)

    assert frame.loc[0, "arXiv ID"] == "2410.03200"
    assert frame.loc[0, "label"] == "1"
