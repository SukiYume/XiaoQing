"""把运行时拥有的全部持久化存储绑定到同一插件数据目录。"""

from __future__ import annotations

from pathlib import Path

from .runtime_state import get_state


def bind_all_stores(state, data_dir: Path) -> None:
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
    state.self_timeline.bind(data_dir)
    state.open_loops.bind(data_dir)
    state.proactive_state.bind(data_dir)
    state.daily_life.bind(data_dir)


def bound_state(context):
    """返回运行时状态，并确保全部存储已绑定到当前插件数据目录。"""
    state = get_state()
    bind_all_stores(state, context.data_dir)
    return state
