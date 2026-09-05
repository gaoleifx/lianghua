# 量化策略自动运行

自动化在A股交易日执行：08:35启动看门狗，15:10停止虚拟盘和实盘策略。看门狗每30秒检查五矿终端、虚拟盘和实盘进程；缺失时执行幂等恢复。策略入口使用角色PID登记与Windows单实例锁，避免重复下单。

## 前置条件

- Windows用户已登录；五矿终端依赖交互桌面和已登录会话。
- 用户环境变量已设置：`GM_TOKEN`、`GOLDMINER_SIM_ACCOUNT`、`GOLDMINER_LIVE_ACCOUNT`。
- 桌面4存在。计划任务可以唤醒睡眠，但不能在用户已注销时运行交互终端。

## 检查与注册

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\start-strategy-auto.ps1 -DryRun
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\watchdog-strategy-auto.ps1 -DryRun
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\automation-selftest.ps1
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\register-automation.ps1
```

## 手动操作

幂等启动；在收盘时间前执行时会自动补齐常驻 watchdog，无需再单独启动监控：

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\start-strategy-auto.ps1
```

也可以直接启动带持续恢复的 watchdog：

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\watchdog-strategy-auto.ps1
```

统一停止自动启动或手动启动并已登记的进程：

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\stop-strategy-auto.ps1
```

日志位于 `logs\automation`，角色PID登记位于项目根目录 `logs\automation_runtime`。失败会写入 `alerts.log`，并在已登录桌面显示本地消息。启动器只有在策略写出 `initialized` 事件且PID登记匹配后才判定健康。
