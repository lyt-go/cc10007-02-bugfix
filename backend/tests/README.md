# 序列挖掘 ↔ 聚类分群衔接修复核验材料（核验码 0270a37d）

本目录是本轮修复的契约核验材料，全部只依赖标准库，可独立复跑。

## 复跑方式

```bash
cd /workspace/backend
# 1. 服务层核验（无需起服务，12 个用例）
.venv/bin/python -I tests/test_sequence_cluster_fix.py
# 2. 原缺陷路径复现（修复前失败、修复后通过）
.venv/bin/python -I tests/repro_bug.py
# 3. HTTP 端到端（先起服务，含真实并发请求）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
.venv/bin/python -I tests/http_e2e_check.py
```

## 需求点 ↔ 核验用例对照

| 需求/核验码 | 核验位置 |
| --- | --- |
| 草稿越级确认被拦，状态严格按生命周期流转（0213113e） | `test_01`、`test_02`、`test_12`、repro_bug 步骤 1 |
| 从暴露错误的事件记录重读事件序列并重算分群特征 | `test_03`、HTTP 步骤 3（`/api/sequence/{id}/events`） |
| 序列状态与聚类输入同事务落库，任一侧失败保留上一份完整结果 | `test_04`（注入落库失败，校验整体回滚） |
| 中断重试成功且不产生重复/半成品版本 | `test_05`、HTTP 步骤 5 |
| 两个确认入口并发只生成一份可复核聚类输入 | `test_06`、HTTP 步骤 4（双线程真实并发） |
| 聚类任务只引用已确认序列版本，冲突原因写入待处理清单 | `test_07`、`test_08`、HTTP 步骤 2（`/api/cluster/pending`） |
| 可从数据编号复查越级确认与未完成特征输出 | `sequence_audit` 冲突留痕 + `cluster_input` 版本编号（CIN-xxxx-vN） |
| 历史聚类输入与旧轮廓系数口径仍可读 | `test_09`、HTTP 步骤 6/8 |
| SEQU-0003 状态回写聚类分群列表；归档/恢复留痕 | `test_10`、`test_11`、HTTP 步骤 6/7 |
| 页面只显示一个完整可执行序列版本及对应明细 | 前端 sequence 页“确认版本”列 + 聚类输入表；`test_06` |
| 收尾先更新当前界面再回清单查关联结果（028e0375/02fa70b3/02294622） | cluster 页“回写序列状态”按钮 + 返回行为序列清单链接 |
| 复现错误时列表/详情丢失错位的处理边界（02c876a1/02bc731f） | repro_bug.py + 行锁/事务边界（`store.row_lock`、`store.transaction`） |

## 留痕数据表（内存仓库，重启回到种子快照）

- `sequence_events`：事件记录，确认时重读重算的唯一口径来源。
- `sequence_audit`：行为序列变更切换、归档与恢复留痕（含冲突）。
- `cluster_input`：聚类输入分群明细数据集，仅已确认版本，旧版标“已替换”保留。
- `cluster_pending`：聚类任务待处理清单，记录消费冲突及原因。
- `cluster_audit`：聚类任务动作留痕。
