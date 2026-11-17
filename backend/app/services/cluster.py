"""聚类分群业务规则：状态流转、字段校验与筛选口径都收在这里。

修复要点（核验码 0270a37d）：

- 聚类任务清单只消费修复后的聚类输入分群明细数据集，待执行任务只引用
  「已确认」序列版本；引用不到或版本不匹配时任务保持待执行，冲突原因写入
  ``cluster_pending`` 聚类任务待处理清单；
- 行为序列（SEQU-0003）的状态通过 ``序列状态`` 字段回写到聚类分群列表，
  序列归档/恢复后再次同步，页面不再显示旧标签；
- 状态机同样按生命周期严格流转：草稿 → 聚类计算中 → 已完成 → 已归档，
  归档结果允许恢复，全程留痕。
"""
from __future__ import annotations

from typing import Any

from app.services import cluster_input
from app.store import now_text, store

MODULE = "cluster"
AUDIT_MODULE = "cluster_audit"
REQUIRED_FIELDS = ["任务编号", "聚类算法", "特征维度"]
STATUS_ORDER = ["草稿", "聚类计算中", "已完成", "已归档"]
ACTION_RULES: dict[str, dict[str, str]] = {
    "启动聚类": {"from": "草稿", "to": "聚类计算中"},
    "确认完成": {"from": "聚类计算中", "to": "已完成"},
    "归档结果": {"from": "已完成", "to": "已归档"},
    "恢复结果": {"from": "已归档", "to": "聚类计算中"},
}
NEGATIVE_ACTIONS: list[str] = []


class ClusterService:
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
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_pending(self, only_open: bool = False) -> list[dict[str, Any]]:
        return cluster_input.list_pending(only_open=only_open)

    def list_inputs(self, sequence_id: int | None = None) -> list[dict[str, Any]]:
        return cluster_input.list_inputs(sequence_id)

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
            entry["目标聚类数"] = values.get("目标聚类数")
            entry["距离度量"] = values.get("距离度量")
            entry["迭代次数上限"] = values.get("迭代次数上限")
            entry["聚类标签"] = "待聚类计算后生成"
            entry["轮廓系数"] = None
            entry["关联序列编号"] = values.get("关联序列编号")
            entry["关联序列版本"] = values.get("关联序列版本")
            entry["关联输入编号"] = None
            entry["序列状态"] = "未关联"
            rows.append(entry)
        self._audit(entry, "登记任务", None, "草稿", ok=True)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"聚类任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于聚类分群可执行范围"
        rule = ACTION_RULES[action]
        with store.row_lock(MODULE, entry_id):
            current = store.find(MODULE, entry_id)
            if current is None:
                return None, f"聚类任务 {entry_id} 不存在或已归档"
            before = str(current.get("status"))
            if before != rule["from"]:
                reason = (
                    f"状态流转冲突：动作「{action}」要求任务处于「{rule['from']}」，"
                    f"当前为「{before}」，已拦截越级推进"
                )
                self._audit(current, action, before, before, ok=False, reason=reason)
                return None, reason
            if action == "启动聚类":
                return self._start(current, before)
            if action == "确认完成":
                return self._finish(current, before)
            return self._advance(current, action, before, rule["to"])

    def sync_sequence_status(self, entry: dict[str, Any]) -> str:
        """把关联序列模式的当前状态回写到聚类任务，供分群列表直接展示。"""
        sequence_code = entry.get("关联序列编号")
        if not sequence_code:
            entry["序列状态"] = "未关联"
            return entry["序列状态"]
        sequence = self._find_sequence_by_code(str(sequence_code))
        if sequence is None:
            entry["序列状态"] = "序列不存在"
        else:
            entry["序列状态"] = str(sequence.get("status"))
        return entry["序列状态"]

    def refresh_all_sequence_status(self) -> int:
        """全量回写一次：序列确认/归档/恢复后让聚类列表拿到最新状态。"""
        count = 0
        for entry in store.rows(MODULE):
            if entry.get("关联序列编号"):
                self.sync_sequence_status(entry)
                count += 1
        return count

    # ---- 内部实现 --------------------------------------------------------

    def _start(self, entry: dict[str, Any], before: str) -> tuple[dict[str, Any], str]:
        """启动聚类：必须能消费到已确认序列版本对应的聚类输入，否则挂待处理。"""
        sequence_code = entry.get("关联序列编号")
        if not sequence_code:
            reason = "任务未关联行为序列模式，缺少聚类输入分群明细数据集，禁止启动"
            with store.transaction():
                cluster_input.add_pending(entry, reason)
                self._audit(entry, "启动聚类", before, before, ok=False, reason=reason)
            return None, reason
        sequence = self._find_sequence_by_code(str(sequence_code))
        if sequence is None:
            reason = f"关联序列 {sequence_code} 不存在，无法取得已确认版本"
            with store.transaction():
                cluster_input.add_pending(entry, reason)
                self._audit(entry, "启动聚类", before, before, ok=False, reason=reason)
            return None, reason
        if str(sequence.get("status")) != "已确认":
            reason = (
                f"关联序列 {sequence_code} 当前为「{sequence.get('status')}」，"
                "待执行聚类任务只允许引用已确认序列版本"
            )
            with store.transaction():
                cluster_input.add_pending(entry, reason)
                entry["序列状态"] = str(sequence.get("status"))
                self._audit(entry, "启动聚类", before, before, ok=False, reason=reason)
            return None, reason
        input_row = cluster_input.latest_input(int(sequence["id"]))
        expected_version = entry.get("关联序列版本")
        if input_row is None:
            reason = f"序列 {sequence_code} 缺少已落库的聚类输入分群明细数据集"
            with store.transaction():
                cluster_input.add_pending(entry, reason)
                self._audit(entry, "启动聚类", before, before, ok=False, reason=reason)
            return None, reason
        if expected_version is not None and int(expected_version) != int(input_row["序列版本号"]):
            reason = (
                f"任务引用序列版本 v{expected_version}，当前有效聚类输入为 "
                f"v{input_row['序列版本号']}，版本冲突，任务保持待执行"
            )
            with store.transaction():
                cluster_input.add_pending(entry, reason)
                self._audit(entry, "启动聚类", before, before, ok=False, reason=reason)
            return None, reason
        with store.transaction():
            entry["status"] = "聚类计算中"
            entry["pending"] = True
            entry["关联序列版本"] = int(input_row["序列版本号"])
            entry["关联输入编号"] = input_row["输入编号"]
            entry["特征维度"] = input_row["分群特征"]
            entry["序列状态"] = "已确认"
            cluster_input.resolve_pending(int(entry["id"]))
            self._audit(
                entry, "启动聚类", before, "聚类计算中", ok=True,
                reason=f"已消费聚类输入 {input_row['输入编号']}",
            )
        return entry, f"聚类任务已启动，消费 {input_row['输入编号']}（仅引用已确认序列版本）"

    def _finish(self, entry: dict[str, Any], before: str) -> tuple[dict[str, Any], str]:
        if not entry.get("关联输入编号"):
            reason = "任务尚未消费任何已确认聚类输入，不能确认完成"
            with store.transaction():
                cluster_input.add_pending(entry, reason)
                self._audit(entry, "确认完成", before, before, ok=False, reason=reason)
            return None, reason
        with store.transaction():
            entry["status"] = "已完成"
            entry["pending"] = False
            # 仅在任务还没有历史结果时生成标签与轮廓系数，旧版本口径保持不变。
            if entry.get("轮廓系数") is None:
                entry["聚类标签"] = self._labels_for(entry)
                entry["轮廓系数"] = self._silhouette_for(entry)
            self.sync_sequence_status(entry)
            self._audit(entry, "确认完成", before, "已完成", ok=True)
        return entry, "聚类任务已确认完成，序列状态已回写聚类分群列表"

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
            self.sync_sequence_status(entry)
            self._audit(entry, action, before, target, ok=True)
        return entry, f"聚类任务已{action}"

    def _find_sequence_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows("sequence"):
            if str(row.get("模式编号")) == code:
                return row
        return None

    def _labels_for(self, entry: dict[str, Any]) -> str:
        return "核心转化群,高频活跃群,加购犹豫群,沉默流失群"

    def _silhouette_for(self, entry: dict[str, Any]) -> float:
        # 确定性占位口径：基于目标聚类数给出稳定值，历史已完成任务不会被重算覆盖。
        k = int(entry.get("目标聚类数") or 4)
        return round(0.5 + 0.04 * (k % 5), 3)

    def _audit(
        self,
        entry: dict[str, Any],
        action: str,
        before: str | None,
        after: str,
        *,
        ok: bool,
        reason: str = "",
    ) -> None:
        store.rows(AUDIT_MODULE).append({
            "id": store.next_id(AUDIT_MODULE),
            "任务id": int(entry["id"]),
            "任务编号": entry.get("任务编号"),
            "动作": action,
            "变更前状态": before,
            "变更后状态": after,
            "关联输入编号": entry.get("关联输入编号"),
            "结果": "成功" if ok else "冲突",
            "原因": reason,
            "操作时间": now_text(),
        })
