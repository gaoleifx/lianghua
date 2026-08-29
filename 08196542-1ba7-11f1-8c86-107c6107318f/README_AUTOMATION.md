# 量化自动运行

## 首次配置

确认当前 Windows 用户已设置 `GM_TOKEN`、`GOLDMINER_SIM_ACCOUNT`、`GOLDMINER_LIVE_ACCOUNT`，并确认桌面4已经存在。启动前会用 AkShare 刷新本地A股交易日缓存；网络不可用时使用上次缓存和 `trading_calendar.json` 中的 `closed_dates`/`open_dates` 覆盖项。

先执行无副作用检查：

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\start-strategy-auto.ps1 -DryRun
```

注册任务：

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\register-automation.ps1
```

## 手动操作

```powershell
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\start-strategy-auto.ps1
PowerShell -NoProfile -ExecutionPolicy Bypass -File .\stop-strategy-auto.ps1
```

日志和运行状态位于 `logs\automation`。停止脚本只清理启动器写入状态文件中的策略 PID，以及本次记录的掘金主进程 PID。
