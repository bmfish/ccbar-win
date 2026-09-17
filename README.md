# ccBar Windows

Windows 系统托盘工具，显示 ccSwitch token 用量。

## 安装

### 一键安装（PowerShell）

```powershell
irm https://raw.githubusercontent.com/bmfish/ccbar-win/master/install.ps1 -OutFile $env:TEMP\install-ccbar.ps1; & $env:TEMP\install-ccbar.ps1
```

### 手动安装

从 [Releases](https://github.com/bmfish/ccbar-win/releases) 下载 `ccBar.exe` 和 `install.ps1`，放在同一目录，右键 `install.ps1` → 使用 PowerShell 运行。

或直接把 `ccBar.exe` 复制到 `%LOCALAPPDATA%\ccBar\` 双击启动。

### 从源码运行

```bash
pip install -r requirements.txt
python main.py
```

### 打包 exe

```bash
pip install pyinstaller
pyinstaller ccBar.spec
```

## 功能

- 左键点击托盘弹出面板：今日用量 / 模型分布 / 近7天 / 近30天 / 历史总量
- 右键菜单：今日 / 昨日 / 近7天 / 近30天 / 历史 详情窗口（带柱状图、折线图、环形图）
- 模型分布详情（按天查看模型占比，环形图 + 列表）
- 托盘图标随用量变色
- 定时备份历史数据（每天 11:00 / 20:00）
- 设置：刷新间隔 / 数据库路径 / 预警阈值

## 数据库路径

默认：`~/.cc-switch/cc-switch.db`

自动识别失败时在设置中手动指定。

## 开发

```bash
pip install -r requirements.txt
python main.py
```

## License

MIT
