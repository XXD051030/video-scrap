# Video Scraper

<img src="Logo.png" alt="Video Scraper 应用图标" width="160" />

粘贴网页或 X（原 Twitter）的帖子链接，在应用内查找影片、播放预览，再下载选中的影片。
界面使用 PyQt6，网页提取使用 yt-dlp、HTML 解析和 Qt WebEngine。

## 当前进度

截至 2026-10-02（UTC+08:00）：

| 模块 | 状态 |
| --- | --- |
| 网页影片提取、预览与下载 | 已实现 |
| X / Twitter 分享链接、多影片选择、播放与下载 | 已实现 |
| 黑白主题切换、设置保存 | 已实现 |
| 应用图标 | 使用仓库根目录的 `Logo.png` |
| macOS Apple Silicon 应用 | 已生成 `.app` 和 ZIP，成品检查通过 |
| Windows x64 应用 | 打包脚本已准备，尚未完成 Windows 实机打包与运行验证 |

当前 Mac 成品已验证：应用启动、主题切换与保存、直链下载的字节一致性、HLS 字节范围下载及合并、实际播放、内嵌网页和 JavaScript。
现有 11 个测试脚本共 90 项测试已在 Mac 的 Python 3.12.14 环境通过。

## 功能

- 粘贴网页链接，尝试查找页面中的影片。
- 粘贴 `x.com` 或 `twitter.com` 帖子分享链接；一个帖子包含多段影片时分别显示。
- 在应用内播放预览，查看封面、时长、分辨率和影片来源。
- 单选影片，下载当前选中的影片。
- 画质选择：best、1080p、720p、480p、audio only；实际可用画质由来源决定。
- 下载进度、日志和自定义保存目录。
- 直链并行下载、HLS 分段下载及 FFmpeg 合并。
- 校验 HTTP 字节范围，避免把错误响应写入分段文件；同名任务独立暂存，完成后避免覆盖已有文件。
- 播放缓冲可在 **Preferences** 中调整：默认 60 秒，范围 0–300 秒；0 表示关闭预读。
- 黑白灰主题：默认深色，右上角 **Light mode / Dark mode** 切换，选择在重启后保留。
- 需要人工验证或登录的网页可使用工具栏的浏览器提取入口。

网页提取按现有流程尝试 yt-dlp、HTML 扫描、页面脚本解码及 WebEngine 渲染。

## 使用方式

1. 粘贴链接，点击 **Scrape** 或按 Enter。
2. 在左侧选择影片。
3. 点击 **Play**，在应用内播放预览。
4. 根据需要选择画质，使用 **Choose…** 设置保存目录。
5. 点击 **Download current** 下载当前影片。

### X / Twitter 登录

如果帖子需要登录，先在受支持的浏览器中登录 X，再从 **X session** 选择该浏览器并重新提取。
应用只在你选择浏览器后读取会话，不要求或保存 X 密码，也不将 Cookie 导出为文件。
受限帖子的可用性取决于当前账户是否有查看权限。

### 保存位置

| 运行方式 | 默认下载目录 |
| --- | --- |
| 直接运行源码 | 当前工作目录的 `downloads/` |
| macOS 应用 | `~/Downloads/Video Scraper` |
| Windows 应用 | `%USERPROFILE%\Downloads\Video Scraper` |

macOS 设置文件位于 `~/Library/Application Support/VideoScraper/settings.json`，Windows 设置文件位于 `%APPDATA%\VideoScraper\settings.json`。
播放缓存放在临时目录，应用正常退出时清理；没有数据库或 Redis 配置。

## 从 GitHub 获取与更新

首次获取：

```bash
git clone https://github.com/XXD051030/video-scrap.git
cd video-scrap
```

已经克隆过项目时，在项目根目录更新：

```bash
git pull --ff-only
```

`Logo.png`、打包脚本和配置均来自仓库。`build/`、`dist/`、虚拟环境和下载文件由 `.gitignore` 排除，需在目标系统重新生成。

## 直接运行源码

需要 Python 3.10 或更新版本，推荐使用 64 位 Python 3.12。
运行依赖见 `requirements.txt`。

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

源码运行时，HLS 合并和部分视频、音频合并功能需要命令行 FFmpeg。
在 Mac 上可以安装：

```bash
brew install ffmpeg
```

下面的打包脚本会把 FFmpeg 一并放入应用；使用打包后的成品无需另装 Python 或 FFmpeg。

## macOS 应用打包

在 Mac 上执行。首次构建先创建独立打包环境：

```bash
python3.12 -m venv build/.venv
build/.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
build/.venv/bin/python scripts/build_macos.py
```

本项目当前机器已有 `build/.venv`，可以直接执行最后一条命令重新打包。
应用使用 `Logo.png` 生成 `.icns` 图标，包含 Python、播放器、Qt WebEngine 和 FFmpeg。

输出：

```text
dist/
├── Video Scraper.app
├── VideoScraper-macOS-arm64.zip
└── build-info.json
```

当前成品为 Apple Silicon 版。脚本按执行它的 Python 架构构建，ZIP 名称也随架构变化。
可双击 `.app` 运行，或将它拖入“应用程序”。

### 系统要求与验证范围

当前成品的二进制最低系统要求为 macOS 13，实际运行验证是在 macOS 27.0.1 上完成。
打包使用 Python 3.12.14，其部署目标为 macOS 11；Qt 将最终最低要求提高到 macOS 13。

构建脚本读取 Python、Qt、FFmpeg 及成品中的实际二进制要求。
如果使用仅面向 macOS 27 编译的 Python，重新构建的应用也会要求 macOS 27。
当前应用使用 ad-hoc 签名，尚未配置 Developer ID 签名和公证。

## Windows 应用打包

在 Windows x64 环境中使用 64 位 Python 3.12。首次构建：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\build_windows.py
```

不需要激活虚拟环境或修改 PowerShell 执行策略。
脚本从 `Logo.png` 生成 `.ico` 图标，并收集 Windows 版 Python、Qt WebEngine、播放器、yt-dlp 和 FFmpeg。

输出：

```text
dist/
├── Video Scraper/
│   ├── Video Scraper.exe
│   └── _internal/
├── VideoScraper-Windows-x64.zip
└── build-info-Windows.json
```

双击 `Video Scraper.exe` 运行。给另一台电脑使用时，发送完整 ZIP，解压后启动。
`.exe` 和 `_internal/` 必须放在同一应用目录，不能只复制 `.exe`。

Windows 脚本已完成语法、图标和配置检查，尚未在 Windows 完成实际打包与成品验证；代码签名也尚未配置。
完整操作与排查步骤见 [Windows 打包指南](WINDOWS_BUILD.md)。
PyInstaller 需要在对应的[目标操作系统](https://pyinstaller.org/en/stable/)中分别构建，不能在 Mac 直接生成 Windows 版。

## 成品离线检查

检查使用临时设置、本机测试网页和自动生成的视频，不读取浏览器登录信息。
它会验证下载字节、HLS 合并、实际播放、网页脚本、主题和设置保存。

### macOS

```bash
"dist/Video Scraper.app/Contents/MacOS/Video Scraper" --smoke-test /tmp/video-scraper-smoke.json
cat /tmp/video-scraper-smoke.json
```

### Windows PowerShell

```powershell
$report = Join-Path $PWD "build\windows\smoke-report.json"
$appProcess = Start-Process -FilePath ".\dist\Video Scraper\Video Scraper.exe" -ArgumentList @("--smoke-test", ('"' + $report + '"')) -Wait -PassThru
$appProcess.ExitCode
Get-Content $report
```

检查通过时退出码为 `0`，报告中的 `ok` 为 `true`。
离线检查通过后，再用自己的网页或 X 分享链接验证真实使用场景。

## 日志与构建记录

| 内容 | 路径 |
| --- | --- |
| Mac 打包日志 | `build/macos/build.log` |
| Windows 打包日志 | `build/windows/build.log` |
| Mac 构建记录 | `dist/build-info.json` |
| Windows 构建记录 | `dist/build-info-Windows.json` |

构建记录包含 Python 和依赖版本、源码及 ZIP 的 SHA-256、Git 提交信息等，方便确认成品来源。
打包工具固定在 `requirements-build.txt`；FFmpeg 来自 [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)。
第三方许可证文本随应用放在 `third-party/` 资源目录。

## 项目结构

```text
video-scrap/
├── main.py                         # 应用入口
├── Logo.png                        # Mac / Windows 共用图标
├── requirements.txt                # 运行依赖
├── requirements-build.txt          # 打包工具
├── README.md
├── WINDOWS_BUILD.md
├── app_bundle/
│   ├── VideoScraper.spec            # Mac 配置
│   ├── VideoScraper-Windows.spec    # Windows 配置
│   └── runtime_hook.py             # 让应用找到内置 FFmpeg
├── scripts/
│   ├── build_macos.py
│   ├── build_windows.py
│   └── smoke_app.py                # 成品离线检查
├── src/
│   ├── scraper.py                  # 网页 / X 影片提取
│   ├── downloader.py               # 下载与 HLS 合并
│   ├── parallel_downloader.py      # 直链并行下载与文件发布
│   ├── media_proxy.py              # 播放代理与缓存
│   ├── settings.py                 # 设置保存
│   ├── js_decoder.py
│   ├── net.py
│   ├── utils.py
│   └── gui/                        # 界面、播放器、浏览器及后台任务
└── tests/                          # 现有回归测试
```

## 已知使用限制

- 是否能提取、播放或下载取决于网页提供的媒体格式、登录权限和网络环境。
- 部分 yt-dlp 来源提供的流不能直接在 Qt 中预览，下载仍可能可用。
- 当前 HLS 处理不支持部分初始化片段与字节范围组合、特殊加密范围格式。无法安全处理时会报错停止。
- Windows 版本仍需要实际构建与运行验证；Mac 通过的测试不能代替 Windows 验证。
