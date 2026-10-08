# linux.do 发帖文案（Windows 托盘版 · v1.7.0 功能对齐版）

> 用法二选一：
> 1. 作为 macOS 版首帖（见 ccbar-native 仓库 `docs/post-linuxdo.md`）的**跟帖更新**——"Windows 版追平 mac 版了"；
> 2. 单独发帖，面向纯 Windows 用户，标题用下面这个。
> 发帖前把 `【截图】` 换成真实截图（建议：托盘变色、面板带每小时折线、洞察中心费用页、
> 洞察页热力图 + 编年史、积分页、设置页主题与双语）。

---

## 标题

Windows 托盘的 AI 用量统计，把 mac 版那套全搬过来了：洞察中心 / 周报 / 主题 / 双语（开源）

---

## 正文

之前发过 macOS 菜单栏版的 CCBar（用量计数器那个），Windows 版一直是"能用"的状态；
这次把 mac 版**最近几个版本的功能全量补上**了，两边现在基本一模一样：

【截图 1：托盘图标 + 悬停数字（变色）】

**洞察中心（六页）**
- 费用：今日 / 7 天 / 30 天费用、**月度预算进度条 + 日预算虚线**、模型费用排行、
  **性价比榜（多少 token 换 1 美元）**；ZCode 这种官方没计费的渠道可以配个默认单价，
  卡片上分开显示「实测 $a · 估算 $b」
- 洞察：连续使用 / 今日会话（几次、平均多久）/ 周环比 / 日均 / 单日峰值 / 最活跃时段 /
  预计本月消耗 / **星期分布** / **近 90 天热力图**（鼠标悬停看每天多少）/ **模型编年史**
- 分享：**今日战报卡**和**上周周报卡**，都带二维码，一键存图发群里
- 渠道：近 30 天渠道与应用堆叠、Token 构成、缓存命中率
- 流水：**任意日期回看**，按渠道/模型筛，能导 CSV
- 积分：Trae 的积分消耗 + 官方账单余额 + 近 30 天走势

【截图 2：洞察中心费用页（预算 + 性价比榜）】

**用量周报**：每周一自动生成上周周报到 `~/Documents/CCBar 周报/`，周一没开机下次开机自动补，
出完弹个通知。

**主题**：6 套内置配色（默认 / 卡哇伊 01 / 海蓝 / 翠绿 / 星空紫 / CRT 终端），
还能导入 / 导出 JSON 主题包——和 mac 版**互通**，欢迎来分享配色。

**中英双语**：中文 / English / 跟随系统，设置里一键切。

**数据安全**：每天自动备份统计库（滚动留 7 份）；明细 CSV 幂等导入导出（重复导零新增，
兼容老版本 12 列文件）；**多机合并**——把另一台机器的 `ccbar.db` 丢进来按主键去重合并。

【截图 3：设置页（主题 / 双语 / 预算 / 数据源）】

**数据源**：cc-switch（Claude Code / Codex / OpenCode 走代理的都在里面）+ ZCode + **Trae**
（Trae 本地库是加密的，走官方接口，粘一次 sessionid 就能自动续期）。
设置里各自勾选启用、路径可改，保存前即时校验。

**口径**（能直接对账的那种）：
| 源 | 口径 |
|---|---|
| cc-switch | input + output + 缓存读 + 缓存创建（全量，费用按它记录的单价折算） |
| ZCode | input + output（与 ZCode 官方统计一致） |
| Trae | 净输入（总量 − 缓存命中）+ output，积分为官方口径 |

架构还是那套：ccbar 自己维护一个本地统计库，历史每天从各源库**只读**同步进来
（INSERT OR IGNORE 幂等去重），"今日"实时查询拼接——源库被清理也不丢历史，
对 cc-switch / ZCode / Trae 零侵入。这次还加了个不变量测试，用文件哈希 + mtime + 行数
把"源库全程只读"钉死在 CI 上。

- 下载：https://github.com/bmfish/ccbar-win/releases （exe 由 GitHub Actions 在 Windows 环境构建）
- 或 PowerShell 一键：`irm https://raw.githubusercontent.com/bmfish/ccbar-win/master/install.ps1 -OutFile $env:TEMP\install-ccbar.ps1; & $env:TEMP\install-ccbar.ps1`
- 源码：https://github.com/bmfish/ccbar-win （Python + pystray + tkinter，`python -m unittest discover -s tests` 可跑测试）
- macOS 版：https://github.com/bmfish/ccbar-native

完全开源、纯本地运行、不上报数据。Windows 佬友试试，想要统计其他 agent 的直接提 issue——
数据源是插件式的，本地有用量数据的工具都好接。

---

## 跟帖备用回复

- **问：和 cc-switch 冲突吗？**
  只读 ATTACH 它的库，不写任何东西；历史存在 ccbar 自己的 `~/.ccbar/ccbar.db`。
  有单测专门盯着"源库字节不被改"这件事。

- **问：exe 报毒 / SmartScreen 拦截？**
  PyInstaller 打包的常见误报，源码全开源可自行构建（`build.bat`），介意的话自己打一遍。

- **问：能统计 X 吗？**
  - 本地有 SQLite 库的：实现 `stats_store.py` 里 `SourceAdapter` 的三个方法即可；
  - 本地库加密、只有 HTTP 接口的：照 `trae_sync.py` 写一个同步器（登录态 + 分页 + 口径归一化）。

- **问：老版本升级会丢配置吗？**
  不会。老的 `~/.ccbar/settings.txt` 首次启动自动迁移到 `settings.json`（含 GBK 中文路径），只迁一次。

- **问：主题包怎么分享？**
  设置 → 主题风格 → 导出，会得到一个 `ccbar-theme-*.json`；别人导入即可，
  mac 版和 Windows 版都能读（字段格式一致）。
