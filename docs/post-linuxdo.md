# linux.do 发帖文案（Windows 托盘版）

> 用法二选一：
> 1. 作为 macOS 版首帖（见 ccbar-native 仓库 `docs/post-linuxdo.md`）的**跟帖更新**——"Windows 版来了"；
> 2. 单独发帖，面向纯 Windows 用户，标题用下面这个。
> 发帖前把 `【截图】` 换成真实截图（托盘变色、面板、渠道分组各一张）。

---

## 标题

Claude Code / ZCode 的 token 用量，Windows 托盘里也能盯了（开源）

---

## 正文

之前发过 macOS 菜单栏版的 CCBar（用量计数器那个），不少佬友问 Windows 什么时候有——**现在有了**，托盘常驻，和 mac 版同一套架构：

【截图 1：托盘图标 + 悬停数字（变色）】

- **托盘图标随今日用量变色**（绿 → 黄 → 橙 → 红），悬停显示数字，超阈值弹 Windows 原生通知
- **左键面板**：今日卡片 / 模型分布 / 近7天 / 近30天 / 历史总量，深色 UI
- **右键详情窗口**：每小时分布、周/月柱状图、模型分布详情（环形图整体分布 + 列表按数据源分组）
- **数据源**：cc-switch（Claude Code / Codex / OpenCode 走代理的都在里面）+ ZCode，设置里各自勾选启用、路径可改；ZCode 按官方口径统计，数字能和它的用量页直接对账
- 每 1000 万 token 一个里程碑 Toast，"今天又烧了一个亿"的实感

【截图 2：左键面板】

【截图 3：模型分布详情按渠道分组】

架构和 mac 版一样：ccbar 自己维护一个本地统计库，历史每天从各源库**只读**同步进来（INSERT OR IGNORE 幂等去重），"今日"实时查询拼接——源库被清理也不丢历史，对 cc-switch / ZCode 零侵入。

- 下载：https://github.com/bmfish/ccbar-win/releases （exe 由 GitHub Actions 在 Windows 环境构建）
- 或 PowerShell 一键：`irm https://raw.githubusercontent.com/bmfish/ccbar-win/master/install.ps1 -OutFile $env:TEMP\install-ccbar.ps1; & $env:TEMP\install-ccbar.ps1`
- 源码：https://github.com/bmfish/ccbar-win （Python + pystray + tkinter）
- macOS 版：https://github.com/bmfish/ccbar-native

完全开源、纯本地运行、不上报数据。Windows 佬友试试，想要统计其他 agent 的直接提 issue——数据源是插件式的，本地有用量数据的工具都好接。

---

## 跟帖备用回复

- **问：和 cc-switch 冲突吗？**
  只读 ATTACH 它的库，不写任何东西；历史存在 ccbar 自己的 `~/.ccbar/ccbar.db`。

- **问：exe 报毒 / SmartScreen 拦截？**
  PyInstaller 打包的常见误报，源码全开源可自行构建（`build.bat`），介意的话自己打一遍。

- **问：能统计 X 吗？**
  实现 `stats_store.py` 里 `SourceAdapter` 的三个方法就能接，本地有用量数据的工具都行。
