"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

修复（核验码 0270a37d / 0213113e）后新增两件基础设施：

1. ``transaction``：进入时对全表做深拷贝快照，事务内任一侧落库失败都会回滚到
   上一份完整结果，保证“序列状态与聚类输入落库必须一起成功”。
2. ``row_lock``：按模块+记录编号加锁，两个确认入口并发到达同一序列模式时被
   串行化，只可能产出一份可复核的聚类输入分群明细数据集。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 内部留痕/衔接表：不对外出现在运营概览的业务模块清单里。
INTERNAL_TABLES = frozenset({
    "sequence_events",
    "sequence_audit",
    "cluster_input",
    "cluster_pending",
    "cluster_audit",
})


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 按 (模块, 记录编号) 维度的互斥锁，串行化同一条记录上的并发动作。
        self._row_locks: dict[tuple[str, int], threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    @contextmanager
    def row_lock(self, module: str, entry_id: int) -> Iterator[None]:
        """串行化同一条记录上的并发请求；不同记录之间互不阻塞。"""
        key = (module, entry_id)
        with self._locks_guard:
            lock = self._row_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._row_locks[key] = lock
        with lock:
            yield

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务边界：块内抛异常时把所有表恢复到进入前的完整快照。

        序列状态写入与聚类输入落库共用一个事务，任一侧失败都保留上一份完整
        结果，不会出现“状态已推进、特征没写完”的半成品。
        """
        snapshot = {name: copy.deepcopy(rows) for name, rows in self._tables.items()}
        try:
            yield
        except Exception:
            self._tables.clear()
            self._tables.update(snapshot)
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            if name in INTERNAL_TABLES:
                # 事件/审计/聚类输入等待处理表是内部留痕，不进业务模块概览。
                continue
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


def now_text() -> str:
    """统一的留痕时间戳，方便审计记录按时间复核。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


store = Store()
