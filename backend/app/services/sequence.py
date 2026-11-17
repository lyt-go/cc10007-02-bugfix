"""行为序列业务规则：状态流转、字段校验与筛选口径都收在这里。

修复要点（核验码 0213113e / 0270a37d）：

- 状态机严格按生命周期流转：草稿 → 挖掘中 → 已确认 → 已归档，不再允许
  “草稿”越级执行“确认模式”，挖掘中也不能读到未完成的序列模式；
- “确认模式”在单条记录锁 + 单一事务内完成：重读事件记录、重算分群特征、
  推进状态、落库聚类输入、写审计留痕，任一步失败整体回滚到上一份完整结果；
- 两个确认入口并发到达同一序列模式时被行锁串行化，只生成一份可复核的
  聚类输入分群明细数据集，第二个请求拿到明确的冲突提示而不是再写一版；
- 变更切换、归档与恢复全部写 ``sequence_audit``，可按数据编号复查。
"""
from __future__ import annotations

from typing import Any

from app.services import cluster_input
from app.store import now_text, store

MODULE = "sequence"
AUDIT_MODULE = "sequence_audit"
REQUIRED_FIELDS = ["模式编号", "用户分群", "事件序列"]
STATUS_ORDER = ["草稿", "挖掘中", "已确认", "已归档"]
# 动作只允许从“紧邻的上一状态”进入目标状态，杜绝越级。
ACTION_RULES: dict[str, dict[str, str]] = {
    "发起挖掘": {"from": "草稿", "to": "挖掘中"},
    "确认模式": {"from": "挖掘中", "to": "已确认"},
    "归档模式": {"from": "已确认", "to": "已归档"},
    "恢复模式": {"from": "已归档", "to": "挖掘中"},
}
NEGATIVE_ACTIONS: list[str] = []


class SequenceService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模式编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_audit(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if entry_id is not None:
            rows = [row for row in rows if int(row.get("序列id", 0)) == entry_id]
        return sorted(rows, key=lambda row: int(row.get("id", 0)))

    def list_events(self, entry_id: int) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows("sequence_events")
            if int(row.get("序列id", 0)) == entry_id
        ]
        return sorted(rows, key=lambda row: int(row.get("事件序号", 0)))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.row_lock(MODULE, -1):
            rows = store.rows(MODULE)
            entry = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["最大步长"] = values.get("最大步长")
            entry["最小支持度"] = values.get("最小支持度")
            entry["时间窗口天"] = values.get("时间窗口天")
            entry["序列频次"] = int(values.get("序列频次") or 0)
            entry["转化率"] = 0.0
            entry["版本号"] = 0
            entry["已确认版本"] = None
            entry["最近确认时间"] = None
            rows.append(entry)
        self._audit(entry, "登记模式", None, "草稿", version=0, ok=True)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"序列模式 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于行为序列可执行范围"
        rule = ACTION_RULES[action]
        # 同一序列模式的两个确认入口并发到达时，在这里被串行化，只产出一个版本。
        with store.row_lock(MODULE, entry_id):
            current = store.find(MODULE, entry_id)
            if current is None:
                return None, f"序列模式 {entry_id} 不存在或已归档"
            before = str(current.get("status"))
            if before != rule["from"]:
                reason = (
                    f"状态流转冲突：动作「{action}」要求序列处于「{rule['from']}」，"
                    f"当前为「{before}」，已拦截越级推进"
                )
                self._audit(current, action, before, before,
                            version=int(current.get("版本号", 0)), ok=False, reason=reason)
                return None, reason
            if action == "确认模式":
                return self._confirm(current)
            return self._advance(current, action, before, rule["to"])

    # ---- 内部实现 --------------------------------------------------------

    def _advance(
        self,
        entry: dict[str, Any],
        action: str,
        before: str,
        target: str,
    ) -> tuple[dict[str, Any], str]:
        with store.transaction():
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            self._audit(entry, action, before, target,
                        version=int(entry.get("版本号", 0)), ok=True)
        return entry, f"序列模式已{action}"

    def _confirm(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """确认模式：状态推进与聚类输入落库在同一事务里一起成功。"""
        sequence_id = int(entry["id"])
        before = str(entry["status"])
        try:
            with store.transaction():
                # 1. 从暴露错误的事件记录里重读事件序列并重算分群特征。
                features = cluster_input.recompute_features(sequence_id)
                # 2. 先写序列字段（版本号 + 重算口径），再提交聚类输入。
                new_version = int(entry.get("版本号", 0)) + 1
                entry["版本号"] = new_version
                entry["已确认版本"] = new_version
                entry["最近确认时间"] = now_text()
                entry["status"] = "已确认"
                entry["pending"] = False
                entry["abnormal"] = False
                entry["事件序列"] = features["事件序列快照"]
                entry["转化率"] = features["转化率"]
                entry["序列频次"] = int(entry.get("序列频次") or 0)
                # 3. 聚类输入分群明细数据集与序列状态一起落库。
                input_row = cluster_input.build_confirmed_input(entry, features)
                self._audit(
                    entry, "确认模式", before, "已确认",
                    version=new_version, ok=True,
                    reason=f"已生成聚类输入 {input_row['输入编号']}",
                )
        except Exception as exc:  # 任一侧失败：事务回滚，保留上一份完整结果
            reason = f"确认失败并已整体回滚：{exc}"
            stale = store.find(MODULE, sequence_id)
            if stale is not None:
                self._audit(stale, "确认模式", before, before,
                            version=int(stale.get("版本号", 0)), ok=False, reason=reason)
            return None, reason
        return entry, (
            f"序列模式已确认模式，版本 v{entry['版本号']}，"
            f"聚类输入分群明细数据集已生成（{input_row['输入编号']}）"
        )

    def _audit(
        self,
        entry: dict[str, Any],
        action: str,
        before: str | None,
        after: str,
        *,
        version: int,
        ok: bool,
        reason: str = "",
    ) -> None:
        store.rows(AUDIT_MODULE).append({
            "id": store.next_id(AUDIT_MODULE),
            "序列id": int(entry["id"]),
            "模式编号": entry.get("模式编号"),
            "动作": action,
            "变更前状态": before,
            "变更后状态": after,
            "版本号": version,
            "结果": "成功" if ok else "冲突",
            "原因": reason,
            "操作时间": now_text(),
        })
