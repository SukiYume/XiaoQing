"""把运行时拥有的全部持久化存储绑定到同一插件数据目录。"""

from __future__ import annotations

from pathlib import Path


def _bind_all_stores(state, data_dir: Path) -> None:
    """绑定所有存储模块到数据目录"""
    state.memory_store.bind_data_dir(data_dir)
    state.media_store.bind(data_dir)
    state.action_history.bind(data_dir)
    state.heartflow.bind(data_dir)
    state.goal_store.bind(data_dir)
    state.review_store.bind(data_dir)
    state.pfc_state_store.bind(data_dir)
    state.bw_expr_store.bind(data_dir)
    state.bw_tracker_store.bind(data_dir)
    state.bw_recorder.bind(data_dir)
    state.bw_jargon_store.bind(data_dir)
    state.memory_db.bind(data_dir)
