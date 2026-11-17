"""聚类输入分群明细数据集服务。

职责（核验码 0270a37d）：

1. 确认序列模式时，从 ``sequence_events`` 事件记录重读事件序列并重新计算分群
   特征，绝不直接采信页面上的旧字段；
2. 每次确认产出一条不可变的聚类输入版本（``cluster_input``），与序列状态写入
   共用同一事务，任一侧失败都会整体回滚到上一份完整结果；
3. 聚类任务消费时只允许引用已确认序列版本，引用不到就把冲突原因写入
   ``cluster_pending`` 待处理清单，任务保持待执行。
"""
from __future__ import annotations

from typing import Any

from app.store import now_text, store

INPUT_MODULE = "cluster_input"
PENDING_MODULE = "cluster_pending"
EVENT_MODULE = "sequence_events"
SEQUENCE_MODULE = "sequence"

# 转化类事件：命中这些事件才算一次转化，用于统一口径重算转化率。
CONVERSION_EVENTS = ("提交订单", "支付成功", "签约成功", "续费成功")


def _load_event_sequence(sequence_id: int) -> list[dict[str, Any]]:
    """从事件记录表重读某条序列模式的完整事件序列，按事件序号排序。"""
    events = [
        row for row in store.rows(EVENT_MODULE)
        if int(row.get("序列id", 0)) == sequence_id
    ]
    return sorted(events, key=lambda row: int(row.get("事件序号", 0)))


def recompute_features(sequence_id: int) -> dict[str, Any]:
    """重读事件序列并计算分群特征。

    返回的特征同时覆盖事件步数、事件种类数、序列频次、转化率与事件序列快照，
    供序列确认写库和聚类输入落库共用，避免两侧口径不一致。
    """
    events = _load_event_sequence(sequence_id)
    if not events:
        raise ValueError(f"序列 {sequence_id} 没有可重读的事件记录，无法计算分群特征")
    names = [str(row.get("事件名称", "")).strip() for row in events]
    step_count = len(names)
    distinct_count = len(set(names))
    conversion_hits = sum(1 for name in names if name in CONVERSION_EVENTS)
    conversion_rate = round(conversion_hits / step_count, 4)
    snapshot = ",".join(names)
    return {
        "事件步数": step_count,
        "事件种类数": distinct_count,
        "转化率": conversion_rate,
        "事件序列快照": snapshot,
        "事件明细": [{"序号": e.get("事件序号"), "事件": e.get("事件名称"), "时间": e.get("事件时间")} for e in events],
    }


def latest_input(sequence_id: int) -> dict[str, Any] | None:
    """读取某条序列当前有效的聚类输入版本（不存在返回 None）。"""
    candidates = [
        row for row in store.rows(INPUT_MODULE)
        if int(row.get("序列id", 0)) == sequence_id and row.get("状态") == "有效"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: int(row.get("序列版本号", 0)))


def build_confirmed_input(sequence: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    """在当前事务内构造并落库一条新的、不可变的聚类输入版本。

    旧版本置为“已替换”但保留可读，兼容历史聚类输入分群明细数据集核对口径；
    旧序列版本的轮廓系数等结果字段不会被覆盖。
    """
    sequence_id = int(sequence["id"])
    version = int(sequence.get("版本号", 1))
    rows = store.rows(INPUT_MODULE)
    for old in rows:
        if int(old.get("序列id", 0)) == sequence_id and old.get("状态") == "有效":
            old["状态"] = "已替换"
    frequency = int(sequence.get("序列频次") or 0)
    entry = {
        "id": store.next_id(INPUT_MODULE),
        "输入编号": f"CIN-{sequence_id:04d}-v{version}",
        "序列id": sequence_id,
        "模式编号": sequence.get("模式编号"),
        "序列版本号": version,
        "事件步数": features["事件步数"],
        "事件种类数": features["事件种类数"],
        "序列频次": frequency,
        "转化率": features["转化率"],
        "分群特征": (
            f"事件步数={features['事件步数']};"
            f"事件种类数={features['事件种类数']};"
            f"序列频次={frequency};"
            f"转化率={features['转化率']:.4f}"
        ),
        "事件序列快照": features["事件序列快照"],
        "状态": "有效",
        "生成时间": now_text(),
    }
    rows.append(entry)
    return entry


def list_inputs(sequence_id: int | None = None) -> list[dict[str, Any]]:
    rows = store.rows(INPUT_MODULE)
    if sequence_id is not None:
        rows = [row for row in rows if int(row.get("序列id", 0)) == sequence_id]
    return sorted(rows, key=lambda row: (int(row.get("序列id", 0)), int(row.get("序列版本号", 0))))


def add_pending(task: dict[str, Any], reason: str, *, conflict: bool = True) -> dict[str, Any]:
    """把聚类任务的消费冲突写入待处理清单，任务本身保持待执行。"""
    entry = {
        "id": store.next_id(PENDING_MODULE),
        "任务id": int(task["id"]),
        "任务编号": task.get("任务编号"),
        "关联序列编号": task.get("关联序列编号"),
        "关联序列版本": task.get("关联序列版本"),
        "冲突原因": reason,
        "是否冲突": conflict,
        "处理状态": "待处理",
        "登记时间": now_text(),
    }
    store.rows(PENDING_MODULE).append(entry)
    return entry


def list_pending(only_open: bool = False) -> list[dict[str, Any]]:
    rows = store.rows(PENDING_MODULE)
    if only_open:
        rows = [row for row in rows if row.get("处理状态") == "待处理"]
    return sorted(rows, key=lambda row: int(row.get("id", 0)))


def resolve_pending(task_id: int) -> None:
    """任务成功消费后，关闭它在待处理清单里的条目，保留留痕。"""
    for row in store.rows(PENDING_MODULE):
        if int(row.get("任务id", 0)) == task_id and row.get("处理状态") == "待处理":
            row["处理状态"] = "已解决"
            row["解决时间"] = now_text()
