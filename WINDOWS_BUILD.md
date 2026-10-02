# Windows 版打包步骤

## 1. 准备 Windows 环境

这套脚本生成 Windows x64 版，建议使用 Windows 11 和 64 位 Python 3.12。
需要在 Windows 电脑或 Windows 虚拟机内执行；PyInstaller 不能在 macOS 上直接生成 Windows 程序。
参考：[PyInstaller 官方说明](https://pyinstaller.org/en/stable/)、[Qt 6.11 支持的平台](https://doc.qt.io/qt-6.11/supported-platforms.html)。

从 [Python 官方 Windows 下载页面](https://www.python.org/downloads/windows/) 安装 Python 3.12 的 64 位版本。
安装后重新打开 PowerShell，确认可以运行：

```powershell
py -3.12 --version
```

脚本会检查 Python 是否为 x64。使用 Windows 虚拟机时，也要确保安装的是 x64 Python。

## 2. 把项目源文件复制到 Windows

需要包含以下文件和目录，保持相同的目录结构：

```text
video-scrap/
├── main.py
├── Logo.png
├── requirements.txt
├── requirements-build.txt
├── src/
├── scripts/
│   ├── build_windows.py
│   └── smoke_app.py
└── app_bundle/
    ├── VideoScraper-Windows.spec
    └── runtime_hook.py
```

不用复制 Mac 的 `.venv/`、`build/`、`dist/` 和 `downloads/`。
如果通过 Git 同步，先确认新的打包脚本和 `Logo.png` 已经提交并推送；未提交文件不会出现在另一台电脑上。

在 Windows 的项目文件夹里打开 PowerShell。下面的命令都在项目根目录执行。

## 3. 安装依赖并打包

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

不需要激活虚拟环境，也不需要修改 PowerShell 的执行策略。
脚本自动执行这些步骤：

- 从 `Logo.png` 生成 Windows `.ico` 图标，保留完整图片和透明背景。
- 将 Windows 版 FFmpeg 放入应用内。
- 收集 Python、Qt 播放器、Qt WebEngine、yt-dlp 和相关依赖。
- 生成应用目录、包含整个应用的 ZIP、版本及文件校验信息。

输出：

```text
dist/
├── Video Scraper/
│   ├── Video Scraper.exe
│   └── _internal/
├── VideoScraper-Windows-x64.zip
└── build-info-Windows.json
```

运行 `dist\Video Scraper\Video Scraper.exe`。
给另一台电脑使用时，发送 `VideoScraper-Windows-x64.zip`，解压后再启动应用。
必须保留 `.exe` 和 `_internal/` 的相对位置，单独复制 `.exe` 无法运行。

## 4. 验证成品

先运行内置的离线检查。它使用临时设置、本机测试网页和自动生成的视频，不读取浏览器登录信息：

```powershell
$report = Join-Path $PWD "build\windows\smoke-report.json"
$appProcess = Start-Process -FilePath ".\dist\Video Scraper\Video Scraper.exe" -ArgumentList @("--smoke-test", ('"' + $report + '"')) -Wait -PassThru
$appProcess.ExitCode
Get-Content $report
```

通过时，退出码为 `0`，报告中的 `ok` 为 `true`。
检查覆盖直链下载的字节一致性、HLS 字节范围下载及视频合并、实际播放、网页脚本、主题切换和设置保存。
之后再双击应用，用自己的网页或 X 链接验证真实使用场景。

默认下载目录为 `%USERPROFILE%\Downloads\Video Scraper`。
设置文件位于 `%APPDATA%\VideoScraper\settings.json`。

## 5. 如果失败

打包日志：

```powershell
Get-Content .\build\windows\build.log -Tail 80
```

将日志末尾、离线检查报告或启动报错截图提供出来，可以据此排查。
当前 Windows 脚本已完成语法和配置检查，但尚未在 Windows 上实际打包或运行验证。
应用尚未配置 Windows 代码签名。
