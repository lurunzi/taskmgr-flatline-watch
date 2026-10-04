# 本地工作约束

- 这是 [bigzeze/ScreenWatcher](https://github.com/bigzeze/ScreenWatcher)（MIT）的个人诊断扩展；保留上游 `LICENSE`、源码与图标归属。`upstream` 远程只用于取上游，不推送。
- 运行说明与验证记录以 [`README.md`](README.md) 为准；`UPSTREAM-README.md` 是上游原文，不改写。
- 本机监测程序可能正在运行（计划任务 `TaskmgrFlatlineWatch` 或 `Start.cmd`）。重启前先看 `D:\TaskmgrFreezeCaptures\status.json`，`dump_running` 为 true 时不停止；停止用 `Stop.ps1`，不强杀。
- 证据目录 `D:\TaskmgrFreezeCaptures` 与 `.local/` 里的调查材料不删除、不入库。
- 不改 Defender、驱动、CPU 或性能计数器设置；不重启 Windows。

## 改动约定

本文件是本仓库的规则入口，`CLAUDE.md` 指向这里。只读任务（回答、查阅、评审、诊断）读到上一节为止，不写入仓库。

- 认领：在本机未入库的 `.local/work-inbox/` 建任务目录，写明人类负责人、AI 会话、认领路径与状态；完成后释放。
- 文档：`README.md` 英文整块在前、中文整块在后，以英文为准；改动时两半同步，不删已有内容。
- README 两个语言半区各嵌一张 Archify 流程图；图源、导出与生成回执在 `docs/diagrams/`。改动检测/取证/恢复流程时同步改两份图源，按回执里的命令重新生成并更新回执。
- 检查：`tools\ci-local.ps1`（单元测试 + `compileall`）。没有 GitHub Actions。
- 本机安装：首次 `Setup.cmd`，再 `Install.cmd` 注册登录自启任务；`Autostart.cmd on|off|status` 切换自启动，`Start.cmd` 手动启动。
- 写入边界：提交推送到 `origin`（`lurunzi/taskmgr-flatline-watch`）的 `main`。只在本工作站运行，没有服务器或部署目标。
