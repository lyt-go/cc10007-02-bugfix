"""复现与核验：SEQU-0001 草稿态执行确认模式（核验码 02c876a1）。

修复前：草稿执行「确认模式」直接进入「已确认」，CLUS-0001 随即读到未完成特征。
修复后：越级推进被状态机拦截，必须先走 草稿 → 挖掘中 → 已确认，
且确认时重读事件记录、重算分群特征并同事务落库聚类输入。

直接运行：.venv/bin/python -I tests/repro_bug.py
"""
import sys

sys.path.insert(0, "/workspace/backend")

from app.services.cluster_input import list_inputs  # noqa: E402
from app.services.sequence import SequenceService  # noqa: E402
from app.store import store  # noqa: E402

svc = SequenceService()

# 1. 原错误路径：草稿直接确认，现在必须被拦截。
entry, message = svc.run_action(1, "确认模式")
assert entry is None, "修复失效：草稿仍可越级确认"
row = store.find("sequence", 1)
assert row["status"] == "草稿" and row["版本号"] == 0
print("1) 越级确认已拦截：", message)

# 2. 正确生命周期：发起挖掘后才能确认。
svc.run_action(1, "发起挖掘")
entry, message = svc.run_action(1, "确认模式")
assert entry is not None and entry["status"] == "已确认"
print("2)", message)

# 3. 确认后聚类输入分群明细数据集已生成，特征来自事件记录重读重算。
cin = list_inputs(1)
assert len(cin) == 1 and cin[0]["输入编号"] == "CIN-0001-v1"
print("3) 聚类输入：", cin[0]["分群特征"])
print("结论：草稿越级确认缺陷已修复，序列状态与聚类输入同事务落库。")
