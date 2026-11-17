"""HTTP 端到端核验：通过真实 uvicorn 调用链验证修复效果。

运行前先起服务：.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
然后执行：.venv/bin/python -I tests/http_e2e_check.py
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def action(module: str, entry_id: int, act: str) -> dict:
    _, payload = call("POST", f"/api/{module}/{entry_id}/actions", {"values": {"action": act}})
    return payload


def main() -> None:
    # 1. 复现草稿越级确认（SEQU-0001）
    r = action("sequence", 1, "确认模式")
    assert r["ok"] is False and "越级" in r["message"], r
    print("1) 草稿越级确认被拦截:", r["message"])

    # 2. 挖掘中读取：聚类任务消费挖掘中序列被拒并进待处理清单
    _, created = call("POST", "/api/cluster", {"values": {
        "任务编号": "CLUS-E2E", "聚类算法": "K-Means", "特征维度": "x",
        "关联序列编号": "SEQU-0002",
    }})
    task_id = created["entry"]["id"]
    r = action("cluster", task_id, "启动聚类")
    assert r["ok"] is False and "已确认序列版本" in r["message"]
    _, pending = call("GET", "/api/cluster/pending")
    assert pending["total"] >= 1 and pending["items"][-1]["冲突原因"]
    print("2) 挖掘中序列消费冲突已写入待处理清单:", pending["items"][-1]["冲突原因"])

    # 3. 正常确认 SEQU-0001，校验重读事件重算特征
    assert action("sequence", 1, "发起挖掘")["ok"]
    r = action("sequence", 1, "确认模式")
    assert r["ok"] and r["entry"]["版本号"] == 1 and r["entry"]["转化率"] == 0.2
    print("3)", r["message"])
    _, inputs = call("GET", "/api/sequence/cluster-inputs?sequence_id=1")
    assert inputs["total"] == 1 and inputs["items"][0]["输入编号"] == "CIN-0001-v1"
    _, events = call("GET", "/api/sequence/1/events")
    assert events["total"] == 5
    _, audit = call("GET", "/api/sequence/1/audit")
    # 首次越级确认的冲突留痕也在审计里，成功链路应为挖掘→确认。
    success_actions = [a["动作"] for a in audit["items"] if a["结果"] == "成功"]
    assert success_actions == ["发起挖掘", "确认模式"]
    assert audit["items"][0]["结果"] == "冲突"
    print("   事件记录与审计留痕可按编号复查，输入特征:", inputs["items"][0]["分群特征"])

    # 4. 两个确认入口并发到达同一序列模式（SEQU-0002 种子里已处于挖掘中）
    results: list[dict] = []

    def confirm() -> None:
        results.append(action("sequence", 2, "确认模式"))

    t1 = threading.Thread(target=confirm)
    t2 = threading.Thread(target=confirm)
    t1.start(); t2.start(); t1.join(); t2.join()
    oks = [r for r in results if r["ok"]]
    fails = [r for r in results if not r["ok"]]
    assert len(oks) == 1 and len(fails) == 1, results
    _, inputs2 = call("GET", "/api/sequence/cluster-inputs?sequence_id=2")
    assert inputs2["total"] == 1, inputs2
    print("4) 并发确认：1 成功 1 冲突，仅生成一份输入", oks[0]["message"], "|", fails[0]["message"])

    # 5. 待处理任务在序列确认后重试成功
    r = action("cluster", task_id, "启动聚类")
    assert r["ok"] and r["entry"]["关联输入编号"] == "CIN-0002-v1"
    r = action("cluster", task_id, "确认完成")
    assert r["ok"] and r["entry"]["status"] == "已完成"
    _, pending = call("GET", "/api/cluster/pending")
    assert all(item["处理状态"] == "已解决" for item in pending["items"] if item["任务编号"] == "CLUS-E2E")
    print("5) 中断重试后任务成功消费已确认版本:", r["entry"]["关联输入编号"])

    # 6. SEQU-0003 状态回写聚类分群列表
    call("POST", "/api/cluster/sync-sequence-status")
    _, clist = call("GET", "/api/cluster?keyword=CLUS-0003")
    c3 = clist["items"][0]
    assert c3["序列状态"] == "已确认" and c3["轮廓系数"] == 0.612
    print("6) 聚类列表回写 SEQU-0003 状态:", c3["序列状态"], "| 旧轮廓系数保持:", c3["轮廓系数"])

    # 7. 归档/恢复留痕与页面数据一致性
    r = action("sequence", 3, "归档模式")
    assert r["ok"]
    call("POST", "/api/cluster/sync-sequence-status")
    _, clist = call("GET", "/api/cluster?keyword=CLUS-0003")
    assert clist["items"][0]["序列状态"] == "已归档"
    r = action("sequence", 3, "恢复模式")
    assert r["ok"] and r["entry"]["status"] == "挖掘中"
    _, audit3 = call("GET", "/api/sequence/3/audit")
    assert [a["动作"] for a in audit3["items"]][-2:] == ["归档模式", "恢复模式"]
    print("7) 归档/恢复已留痕并同步聚类列表")

    # 8. 历史聚类输入仍可读（v1 已替换、v2 有效）
    action("sequence", 3, "确认模式")
    _, inputs3 = call("GET", "/api/sequence/cluster-inputs?sequence_id=3")
    statuses = [(i["序列版本号"], i["状态"]) for i in inputs3["items"]]
    assert (1, "已替换") in statuses and (2, "有效") in statuses
    print("8) 历史输入版本可读:", statuses)

    print("\n全部 HTTP 端到端核验通过")


if __name__ == "__main__":
    main()
