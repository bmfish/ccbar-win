# ccBar Windows

Windows 系统托盘工具，显示 ccSwitch token 用量。

## 安装

### 方法一：下载 exe（推荐）
从 [Releases](https://github.com/bmfish/ccbar-win/releases) 下载 `ccBar.exe`，双击运行。

### 方法二：从源码运行
```bash
pip install -r requirements.txt
python main.py
```

### 方法三：打包成 exe
```bash
pip install pyinstaller
pyinstaller --onefile --noconsole main.py
```

## 功能

- 系统托盘显示今日 token 用量
- 悬停显示详细信息
- 右键菜单显示：
  - 今日用量
  - 近7天用量
  - 近30天用量
- 设置刷新间隔（5-3000秒）
- 设置数据库路径

## 数据库路径

默认：`~/.cc-switch/cc-switch.db`

如果自动识别失败，请在设置中手动指定。

## 开发

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

## License

MIT
