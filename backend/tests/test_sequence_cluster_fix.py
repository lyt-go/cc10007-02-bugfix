"""修复核验测试（核验码 0270a37d / 0213113e / 02c876a1）。

覆盖需求点：
1. 草稿确认越级拦截、挖掘中读取拦截、正常生命周期；
2. 确认时从事件记录重读事件序列并重算分群特征；
3. 序列状态与聚类输入同事务落库，任一侧失败回滚上一份完整结果；
4. 两个确认入口并发到达同一序列模式，只生成一份可复核聚类输入；
5. 聚类任务只消费已确认序列版本，冲突写入待处理清单，可按编号复查；
6. 中断重试：失败后重试可成功，且不产生重复/半成品版本；
7. 历史聚类输入与旧轮廓系数口径保持可读；
8. SEQU-0003 状态回写聚类分群列表；归档与恢复留痕。

直接运行：.venv/bin/python -I tests/test_sequence_cluster_fix.py
"""
from __future__ import annotations

import sys
import threading
import unittest

sys.path.insert(0, "/workspace/backend")

from app.services import cluster_input  # noqa: E402
from app.services.cluster import ClusterService  # noqa: E402
from app.services.sequence import SequenceService  # noqa: E402
from app.store import store  # noqa: E402


class SequenceLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从种子快照重建内存仓库，避免互相污染。
        from app.seed import SEED_ROWS
        import copy
        store._tables = {name: copy.deepcopy(rows) for name, rows in SEED_ROWS.items()}
        store._row_locks = {}
        self.seq = SequenceService()
        self.clu = ClusterService()

    def test_01_draft_cannot_confirm_directly(self) -> None:
        """复现路径（02c876a1）：草稿态确认模式必须被拦截，状态留在草稿。"""
        entry, message = self.seq.run_action(1, "确认模式")
        self.assertIsNone(entry)
        self.assertIn("越级", message)
        row = store.find("sequence", 1)
        self.assertEqual(row["status"], "草稿")
        self.assertEqual(row["版本号"], 0)
        # 冲突也要留痕，可按数据编号复查。
        audit = self.seq.list_audit(1)
        self.assertEqual(audit[-1]["结果"], "冲突")

    def test_02_mining_cannot_confirm_without_mining_step(self) -> None:
        """挖掘中序列再次发起挖掘、草稿归档等越级动作同样被拦。"""
        entry, _ = self.seq.run_action(1, "发起挖掘")
        self.assertEqual(entry["status"], "挖掘中")
        again, message = self.seq.run_action(1, "发起挖掘")
        self.assertIsNone(again)
        self.assertIn("状态流转冲突", message)
        # 已确认的 SEQU-0003 不能重复确认。
        dup, message = self.seq.run_action(3, "确认模式")
        self.assertIsNone(dup)
        self.assertIn("已确认", message)

    def test_03_confirm_rereads_events_and_recomputes_features(self) -> None:
        """确认时从事件记录重读，转化率等特征按统一口径重算而非读旧值。"""
        self.seq.run_action(1, "发起挖掘")
        entry, message = self.seq.run_action(1, "确认模式")
        self.assertEqual(entry["status"], "已确认")
        # 5 个事件里只有 1 个转化事件（提交订单）→ 0.2
        self.assertEqual(entry["转化率"], 0.2)
        self.assertEqual(entry["事件序列"], "浏览首页,搜索商品,查看详情,加购,提交订单")
        inputs = cluster_input.list_inputs(1)
        self.assertEqual(len(inputs), 1)
        cin = inputs[0]
        self.assertEqual(cin["输入编号"], "CIN-0001-v1")
        self.assertEqual(cin["事件步数"], 5)
        self.assertEqual(cin["事件种类数"], 5)
        self.assertEqual(cin["序列版本号"], 1)
        self.assertEqual(cin["状态"], "有效")
        self.assertIn("转化率=0.2000", cin["分群特征"])

    def test_04_transaction_rolls_back_when_cluster_input_fails(self) -> None:
        """聚类输入落库失败时，序列状态也必须回滚到上一份完整结果。"""
        self.seq.run_action(1, "发起挖掘")
        original = store.find("sequence", 1)
        before_status = original["status"]
        before_inputs = len(store.rows("cluster_input"))

        def boom(_sequence_id: int):
            raise RuntimeError("模拟聚类输入侧落库失败")

        saved = cluster_input.recompute_features
        cluster_input.recompute_features = boom  # type: ignore[assignment]
        try:
            entry, message = self.seq.run_action(1, "确认模式")
        finally:
            cluster_input.recompute_features = saved  # type: ignore[assignment]

        self.assertIsNone(entry)
        self.assertIn("整体回滚", message)
        row = store.find("sequence", 1)
        self.assertEqual(row["status"], before_status)  # 仍是挖掘中
        self.assertEqual(row["版本号"], 0)
        self.assertEqual(len(store.rows("cluster_input")), before_inputs)  # 无半成品
        # 失败留痕可复查。
        self.assertEqual(self.seq.list_audit(1)[-1]["结果"], "冲突")

    def test_05_retry_after_failure_succeeds_once(self) -> None:
        """中断重试：首次失败后再次确认成功，且只产出一个输入版本。"""
        self.seq.run_action(1, "发起挖掘")
        calls = {"n": 0}
        real = cluster_input.recompute_features

        def flaky(sequence_id: int):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟中断")
            return real(sequence_id)

        cluster_input.recompute_features = flaky  # type: ignore[assignment]
        try:
            entry, message = self.seq.run_action(1, "确认模式")
            self.assertIsNone(entry)
            entry, message = self.seq.run_action(1, "确认模式")
            self.assertIsNotNone(entry)
        finally:
            cluster_input.recompute_features = real  # type: ignore[assignment]
        self.assertEqual(entry["版本号"], 1)  # 失败那次没有占版本号
        self.assertEqual(len(cluster_input.list_inputs(1)), 1)

    def test_06_concurrent_confirms_produce_single_input(self) -> None:
        """两个确认入口并发到达同一序列模式：只生成一份可复核输入。"""
        self.seq.run_action(1, "发起挖掘")
        outcomes: list[tuple[object, str]] = []

        def confirm() -> None:
            outcomes.append(self.seq.run_action(1, "确认模式"))

        t1 = threading.Thread(target=confirm)
        t2 = threading.Thread(target=confirm)
        t1.start(); t2.start(); t1.join(); t2.join()

        ok = [o for o in outcomes if o[0] is not None]
        conflict = [o for o in outcomes if o[0] is None]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(conflict), 1)
        self.assertIn("状态流转冲突", conflict[0][1])
        row = store.find("sequence", 1)
        self.assertEqual(row["版本号"], 1)
        inputs = cluster_input.list_inputs(1)
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["序列版本号"], 1)
        # 页面只能看到一个完整可执行版本。
        self.assertEqual(row["已确认版本"], 1)

    def test_07_cluster_consumes_only_confirmed_versions(self) -> None:
        """聚类任务引用挖掘中序列启动 → 挂待处理清单；确认后才能消费。"""
        task, _ = self.clu.create_entry({
            "任务编号": "CLUS-T1", "聚类算法": "K-Means",
            "特征维度": "x", "关联序列编号": "SEQU-0002",
        })
        entry, message = self.clu.run_action(task["id"], "启动聚类")
        self.assertIsNone(entry)
        pending = self.clu.list_pending(only_open=True)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["任务编号"], "CLUS-T1")
        self.assertIn("已确认序列版本", pending[0]["冲突原因"])
        self.assertEqual(store.find("cluster", task["id"])["status"], "草稿")

        self.seq.run_action(2, "发起挖掘")
        self.seq.run_action(2, "确认模式")
        entry, message = self.clu.run_action(task["id"], "启动聚类")
        self.assertEqual(entry["status"], "聚类计算中")
        self.assertEqual(entry["关联输入编号"], "CIN-0002-v1")
        self.assertEqual(self.clu.list_pending(only_open=True), [])

    def test_08_version_conflict_goes_to_pending_list(self) -> None:
        """任务钉住旧版本，而序列产生新版本时，冲突原因写入待处理清单。"""
        task, _ = self.clu.create_entry({
            "任务编号": "CLUS-T2", "聚类算法": "K-Means",
            "特征维度": "x", "关联序列编号": "SEQU-0003",
            "关联序列版本": 1,
        })
        # 序列归档后恢复再确认，产出 v2。
        self.seq.run_action(3, "归档模式")
        self.seq.run_action(3, "恢复模式")
        self.seq.run_action(3, "确认模式")
        self.assertEqual(store.find("sequence", 3)["版本号"], 2)
        entry, message = self.clu.run_action(task["id"], "启动聚类")
        self.assertIsNone(entry)
        self.assertIn("版本冲突", message)
        pending = self.clu.list_pending(only_open=True)
        self.assertEqual(pending[0]["任务编号"], "CLUS-T2")

    def test_09_legacy_input_and_silhouette_remain_readable(self) -> None:
        """旧聚类输入版本标记已替换但仍可读；旧轮廓系数口径不被覆盖。"""
        legacy = store.find("cluster", 3)
        self.assertEqual(legacy["轮廓系数"], 0.612)
        # SEQU-0003 再确认一版。
        self.seq.run_action(3, "归档模式")
        self.seq.run_action(3, "恢复模式")
        self.seq.run_action(3, "确认模式")
        inputs = cluster_input.list_inputs(3)
        self.assertEqual([i["状态"] for i in inputs], ["已替换", "有效"])
        self.assertEqual(inputs[0]["序列版本号"], 1)  # 历史版本仍按原口径可读
        # CLUS-0003 是已完成任务，轮廓系数保持 0.612 不变。
        self.assertEqual(store.find("cluster", 3)["轮廓系数"], 0.612)

    def test_10_sequence_status_writeback_to_cluster_list(self) -> None:
        """SEQU-0003 复核对象：状态回写聚类分群列表，归档/恢复同步更新。"""
        task, _ = self.clu.create_entry({
            "任务编号": "CLUS-T3", "聚类算法": "K-Means",
            "特征维度": "x", "关联序列编号": "SEQU-0003",
            "关联序列版本": 1,
        })
        self.clu.run_action(task["id"], "启动聚类")
        self.clu.run_action(task["id"], "确认完成")
        self.assertEqual(store.find("cluster", task["id"])["序列状态"], "已确认")

        self.seq.run_action(3, "归档模式")
        self.clu.refresh_all_sequence_status()
        self.assertEqual(store.find("cluster", task["id"])["序列状态"], "已归档")
        self.assertEqual(store.find("cluster", 3)["序列状态"], "已归档")

        self.seq.run_action(3, "恢复模式")
        self.clu.refresh_all_sequence_status()
        self.assertEqual(store.find("cluster", task["id"])["序列状态"], "挖掘中")

    def test_11_archive_restore_and_audit_trail(self) -> None:
        """变更切换、归档与恢复全部留痕，可按编号复查完整链路。"""
        self.seq.run_action(1, "发起挖掘")
        self.seq.run_action(1, "确认模式")
        self.seq.run_action(1, "归档模式")
        self.assertEqual(store.find("sequence", 1)["status"], "已归档")
        entry, _ = self.seq.run_action(1, "恢复模式")
        self.assertEqual(entry["status"], "挖掘中")
        actions = [row["动作"] for row in self.seq.list_audit(1)]
        self.assertEqual(
            actions,
            ["发起挖掘", "确认模式", "归档模式", "恢复模式"],
        )
        results = {row["动作"]: row["结果"] for row in self.seq.list_audit(1)}
        self.assertTrue(all(v == "成功" for v in results.values()))

    def test_12_cluster_state_machine_blocks_skipping(self) -> None:
        """聚类任务自身也不能越级：草稿不能直接确认完成。"""
        task, _ = self.clu.create_entry({
            "任务编号": "CLUS-T4", "聚类算法": "K-Means", "特征维度": "x",
        })
        entry, message = self.clu.run_action(task["id"], "确认完成")
        self.assertIsNone(entry)
        self.assertIn("状态流转冲突", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
