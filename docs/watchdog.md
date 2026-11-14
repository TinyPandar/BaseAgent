# Watchdog

任务级巡检。它看进展、重复趋势、预算和超时，不代替调用前的安全拦截。

## 和 HarnessKnight、M1 的边界

| 组件 | 看什么 | 什么时候动手 | 不做什么 |
| --- | --- | --- | --- |
| HarnessKnight | 单次 prompt / 工具调用的安全策略和污染跟踪 | 工具进入账本之前，ALLOW / REVIEW / BLOCK | 不判断任务是不是停滞 |
| M1 `--repeat-call-guard` | 同一步里完全相同的工具调用 | 第三次拦截这一次调用，不结束任务 | 不看跨步骤趋势 |
| Watchdog | 跨步骤的停滞、重复趋势、任务预算、超时 | 先记一条 hint，超过上限则 `aborted_by_watchdog` | 不再做一套调用前权限引擎 |

Watchdog 只读 `state.metadata["harnessknight"]` 里已经有的决策计数。没有 HarnessKnight 时，这个字段是 `present: false`，不引入 HK 运行时依赖。

M1 拦截保持 opt-in。两者同时打开时，M1 仍拦第三次相同调用；Watchdog 的建议上限是 4，用来结束任务，而不是再拦同一次调用。

## 规则

全部可关。`--watchdog` 打开后使用建议值；某个阈值传 0 表示关闭那一条。

| 规则 | 建议值 | 动作 |
| --- | --- | --- |
| stall | 连续 3 步 hint，5 步停止 | 这些步没有成功写入（`write_file` / `edit_file` / `apply_patch`），也没有 `verify_command` |
| repeat_call | 相同工具 + 等价参数 4 次 | 下一次工具不再执行 |
| budget | 步数 / token / 墙钟，需显式设置 | 剩余不超过 20% 时 hint，用尽则停止 |
| timeout | 单次工具超时，需显式设置 | 超时的下一次工具，尤其是写入，不再执行 |

## 事件

`watchdog_decision` 写入现有事件日志。字段只有规则名、step、动作和证据，不放参数原文。

| 字段 | 含义 |
| --- | --- |
| `rule` | `stall` / `repeat_call` / `budget` / `timeout` |
| `step` | 触发时的逻辑步 |
| `action` | `hint` 或 `stop` |
| `evidence.call_id` | 重复或超时对应的调用 |
| `evidence.budget_remaining` | 步数、token、秒的剩余 |

停止后的 session 状态是 `aborted_by_watchdog`。停止决定之后的写入不会进入账本。

## 演示

```bash
python -B scripts/demo_watchdog.py
```

脚本用假模型反复读同一个文件。Watchdog 在 8 步上限前停止，并打印 stall 次数、重复次数和预算剩余。费用 0。
