# Trigger

只盯一个项目。定时事件和文件变化可以唤醒已有的 qa / coding Worker。默认只读，不启动长期守护进程，也不调用付费模型。

## 订阅

配置是一份可检查的 JSON。`enabled: false` 后不再唤醒。

```json
{
  "enabled": true,
  "debounce_seconds": 1,
  "subscriptions": [
    {"name": "test-failure-summary", "kind": "fs", "glob": "reports/*.log", "worker": "qa", "mode": "readonly"}
  ]
}
```

`kind` 目前只有 `fs` 和 `timer`。短时间里的重复文件事件会合并，只唤醒一次。源码变化默认给 qa 写一段只读说明。测试失败也可以记到 coding，但 Trigger 不会静默改代码；真正修改仍走原来的审批账本。

## 记账

每次投递都留下订阅名、唤醒了谁、动作是 `woke` 还是 `debounced`、摘要和 token。脚本化摘要的 token 是 0。这是 `--inspect-session` 的等价查询：读 `TriggerBus.inspect()`。

## 演示

```bash
python -B scripts/demo_triggers.py
```

它往夹具项目写入一条失败日志，qa 产出只读摘要，`sample.py` 保持原样。费用 0。

不要在没有 Frank 授权时启动真实仓库监听、联网或付费模型的常驻进程。
