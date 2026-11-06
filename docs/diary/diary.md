# Video Scraper 项目进度记录

> 文档生成时间：2026-09-28 21:27 +08:00（Asia/Kuala_Lumpur）  
> 仓库：`/Users/xxd/Playground/video-scrap`  
> 记录范围：最近三项 Git 里程碑（`44069f4`、`40b52d0`、`9b88ea6`），重点记录 2026-09-28 的 Range 校验和同名下载修复。  
> 状态：创建本文档前，`main` 与 `origin/main` 均指向 `9b88ea6`，工作区干净；此前不存在 `diary.md`，本次新建，没有追加旧文件。

## 1. 本轮工作概述

### 时间线

| 时间（Asia/Kuala_Lumpur） | 目的与执行过程 | 最终结果 |
|---|---|---|
| 2026-09-26 21:21 +08:00 | 基础回归与设置可靠性里程碑：检查设置持久化和解析器热点，改进设置写入及解码器索引查找，并补充设置、抓取和下载测试。 | 创建 commit `44069f4`；远端 reflog 记录于 2026-09-26 21:22 +08:00 更新。 |
| 2026-09-27 01:30 +08:00 | 实现 X/Twitter 帖子视频流程：解析帖子中的视频条目、支持从已登录浏览器读取会话、应用内预览并下载选中的视频；更新 README 并增加专项测试。 | 创建 commit `40b52d0`。提交当时留在本地，直到 2026-09-28 推送时才进入远端。 |
| 2026-09-28 14:29–16:59 +08:00 | 复查 Range 字节错位风险。分别检查分段下载器和播放代理；用假响应覆盖服务器忽略 Range、错误 `Content-Range`、短响应、超长响应及压缩响应。代理保持逐块转发，避免等整部媒体下载完才开始播放。 | 增加 Range 校验、缓存边界保护和失败用例；同时验证 Range 预检不支持时会退回单流下载。时间来自对应 Codex 工作轮记录。 |
| 2026-09-28 16:59 +08:00 | 用户要求一并处理同名任务共用 `.part` 的风险。盘点直链、HLS、yt-dlp、X 和 ffmpeg 的输出路径及回退过程。 | 确认问题不止临时文件，也包括同名最终文件及 HLS `.partial.mp4` 派生名冲突；改用每任务独立 staging 目录和不覆盖发布。 |
| 2026-09-28 20:12–20:51 +08:00 | 验证同名顺序/并发下载、`A` 与 `A.partial` 派生名称、HLS 取消保留 partial、后备失败清理、yt-dlp 非零返回码和不支持硬链接时的发布回退。运行测试并复查差异。 | 10 个测试脚本通过；公开 X 帖子的临时下载成功，成品 184,236 字节，staging 目录已清理。真实 HLS 下载未验证，因为环境找不到 `ffmpeg`。分钟范围按该轮执行窗口记录。 |
| 2026-09-28 20:51 +08:00 | 用户要求 commit 和 push；先核对工作区、分支、远端、暂存区及差异。 | 只提交本次 Range/同名下载修复涉及的 7 个代码和测试文件；此前未推送的 X 功能提交也包含在随后推送中。 |
| 2026-09-28 20:53 +08:00 | 创建并推送下载修复 commit。Git 记录 commit 时间为 20:53:04；`origin/main` reflog 记录 push 更新于 20:53:21。 | `9b88ea6` 推送成功；`main` 与 `origin/main` 一致，工作区干净。 |
| 2026-09-28 21:07–21:27 +08:00 | 用户要求整理当前项目进度；读取 Git 历史、工作区状态、项目文件、依赖版本、启动方式、代理端口和设置路径。 | 新建本 `diary.md`，记录事实、限制及待办。 |

## 2. 修复的 Bug

### Bug 1：分段下载和播放代理可能接受错误字节范围

- **现象描述**：分段下载器可能把服务器返回的完整 `200`、错起点/错总长 `206`、短响应或超长响应按请求偏移写入，导致文件内容损坏。播放代理也可能把 `Content-Range` 起点不符的响应体写到请求起点，令缓存区间和实际字节错位。
- **根因分析**：旧代码没有把 HTTP 状态、`Content-Range`、总长、响应字节数与请求范围逐项比对；压缩响应还可能让 `iter_content` 返回的字节和 Range 偏移使用不同表示。
- **修复方式**：Range 请求使用 `Accept-Encoding: identity`；下载前用 `bytes=0-1` 实际探测，只有匹配的 `206 Content-Range` 才并行分段。每段检查范围、总长、响应头长度和实收字节数，短写循环写完；失败时清理本任务临时文件并保留旧成品。代理在缓存前验证范围/编码/总长，逐块限制写入边界；短响应只标记真实收到且位置可信的前缀。服务器忽略 Range 返回 `200` 时，从偏移 0 解释回退数据，已知总长不一致则拒绝混用缓存。
- **涉及文件/代码位置**：`/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:47`、`:60`、`:153`、`:349`；`/Users/xxd/Playground/video-scrap/src/media_proxy.py:139`、`:152`、`:166`、`:468`、`:1269`、`:1390`。测试在 `tests/test_parallel_downloader.py` 与新增 `tests/test_media_proxy_ranges.py`。

### Bug 2：同名下载共用临时文件或覆盖已有成品

- **现象描述**：相同标题的直链任务使用相同 `.part` 路径；新任务可能删除、截断或互相覆盖前一个任务的数据。HLS、ffmpeg、普通 yt-dlp 和 X 下载也可能写入相同最终文件名；HLS 取消生成的 `A.partial.mp4` 还可能与标题为 `A.partial` 的另一视频冲突。
- **根因分析**：输出名仅由标题和扩展名决定，没有任务级隔离；GUI 可同时运行两个下载任务，最终写入会覆盖同名路径。
- **修复方式**：`VideoDownloader.download()` 为每项任务建立独立的 staging 目录，各后端和 HLS 回退全程只写入该目录。任务完成后再以不覆盖已有文件的方式发布；同名时自动编号。直链每次生成独立随机 `.part`，发布优先用原子硬链接；文件系统不支持硬链接时，用 `O_EXCL` 占位再 `os.replace`。HLS 后备失败会清除本任务暂存文件，避免半成品被后续后备误认为成功。
- **涉及文件/代码位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:201`、`:239`、`:247`；`/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:60`、`:113`、`:153`。新增 `tests/test_download_name_reservation.py`，并更新 `tests/test_parallel_downloader.py`。

### Bug 3：yt-dlp 返回失败状态时可能发布失败产物

- **现象描述**：普通 yt-dlp 或原生 HLS 即使 `ydl.download()` 返回非零状态，若进度回调留下文件，旧流程仍可能把文件当作完成结果。
- **根因分析**：普通和 HLS yt-dlp 路径未统一检查下载返回值；普通流程也可能把合并前文件路径留作结果。
- **修复方式**：普通和 HLS yt-dlp 检查非零返回码并拒绝发布；通过 post hook 获取合并后的成品路径。任务 staging 目录在异常时清理。
- **涉及文件/代码位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:312`、`:371`、`:374`、`:773`、`:859`、`:865`；测试见 `tests/test_download_name_reservation.py` 和 `tests/test_x_downloader.py`。

## 3. 遇到的问题与解法

| 时间 | 问题与排查 | 解决方式与结果 | 状态 |
|---|---|---|---|
| 2026-09-28 14:29–16:59 +08:00 | 有些服务器 HEAD 声称支持 Range，但实际 GET 忽略 Range 并返回完整 `200`；直接分段写入会错位。 | 增加 `bytes=0-1` 流式探测；有效 `206` 才分段，否则用单流下载。覆盖一字节文件和无效探针回退测试。 | 已解决 |
| 2026-09-28 14:29 +08:00 `[待确认：单条命令分钟未记录]` | 默认沙箱运行 `ThreadingHTTPServer(('127.0.0.1', 0))` 时收到 `PermissionError: [Errno 1] Operation not permitted`；测试尚未进入代理代码路径。 | 这是本机回环监听权限限制；在允许本机回环监听的测试调用中重跑，退出码为 0。命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_x_preview.py`。 | 已解决 |
| 2026-09-28 20:12–20:51 +08:00 `[待确认：单次测试分钟未记录]` | 默认受限网络无法解析 `api.x.com`，报 `NameResolutionError`。 | 在允许网络访问的执行环境重跑公开测试帖；下载成功，文件 184,236 字节，staging 目录清理完成。使用 `.venv/bin/python` 调用 `VideoScraper` 与 `VideoDownloader` 的临时脚本。 | 已解决；依赖网络可用 |
| 2026-09-28 20:12–20:51 +08:00 `[待确认：模拟测试分钟未记录]` | 部分外接盘/共享卷不支持 `os.link`；仅用硬链接会让已完成下载在发布阶段失败。 | 增加独占占位文件加 `os.replace` 回退，并用 `EPERM`、`EIO` 等模拟不支持/失败路径；确认旧文件不变、占位可清理。命令：`.venv/bin/python tests/test_parallel_downloader.py` 和 `.venv/bin/python tests/test_download_name_reservation.py`。真实外接盘未实测。 | 软件回退已实现；物理卷兼容性待实测 |
| 2026-09-28 21:07–21:16 +08:00 | 用户要求参考终端历史。当前 Codex 命令执行是非交互会话，无法读取用户私有 `~/.zsh_history`。 | 以 `git log`、`git show`、`git reflog`、仓库文件清单、本轮工具执行输出和对话记录追溯；没有根据猜测补写 shell 命令。 | `[待确认]` 若需逐条核对用户本机 shell 历史，需用户提供相关片段 |

### 已知但本轮未修复的风险

这些风险来自此前的只读代码复核，commit `9b88ea6` 未覆盖：

| 优先级 | 未解决项 | 位置、影响和下一步 |
|---|---|---|
| P1（平台兼容） | `[未解决]` Windows 上 `os.pwrite` 可用性未解决。 | `src/parallel_downloader.py:269` 调用 `os.pwrite`；当前仅在 macOS 环境验证。若继续承诺 Windows 直接下载，需要实现可移植定位写入并在 Windows runner 验证。 |
| P1（资源占用） | `[未解决]` 代理收到忽略 Range 的 `200` 且没有 `Content-Length` 时仍可能读取/缓存完整媒体；持续 `403` 的部分预取路径可能高频重试。 | `src/media_proxy.py:468`、`:492`、`:1312`。下一步为无长度响应设置缓存边界，并为 HTTP 状态错误加入退避与回归测试。 |
| P1（HLS 兼容） | `[未解决]` 原生 HLS 解析器未处理 `EXT-X-BYTERANGE`。 | `src/downloader.py:620` 和 `:658` 会跳过未识别标签并按完整 URI 下载。下一步解析字节范围或把此清单安全交给 yt-dlp；需真实 HLS 样本验证。 |
| P2（抓取准确性） | `[未解决]` HTML 重定向后的相对链接仍按输入 URL 拼接；iframe 主机识别有子串误判；`.mp4?x=.m3u8`/`.mpd` 分类可能不准确。 | `src/scraper.py:104`、`:183`、`:697`–`:783`。下一步依据最终响应 URL 和解析后的 hostname/path 加回归测试。 |
| P2（GUI 时序与显示） | `[未解决]` JS 回退回调可能读取后来更新的 `_current_url`；预览富文本直接插入网页 URL；缩略图切换仍可能强制终止 worker。 | `src/gui/main_window.py:510`、`:590`–`:598`；`src/gui/preview_panel.py:316`–`:323`、`:474`–`:483`；`src/gui/video_list.py:176`。下一步增加抓取代次标识、转义链接并改为协作式 worker 取消。 |
| P2（交互浏览器线程） | `[未解决]` WebEngine 请求拦截路径存在跨线程更新控件风险，尚无 GUI 实机复现。 | `src/gui/interactive_scraper.py:147`、`:245`、`:285`。下一步通过 Qt signal 把 UI 更新投递到主线程并做 WebEngine 实机验证。 |
| P2（取消下载） | `[未解决]` 普通 yt-dlp hook 未检查取消标记，点击停止后可能要等 yt-dlp 自行结束；窗口关闭仍有 QThread 超时强制终止路径。 | `src/downloader.py:312`–`:343`；`src/gui/main_window.py:1162`。下一步实现可中断下载并补取消/关窗回归。 |
| P3（外部进程极端竞态） | `[未解决]` 无硬链接卷回退在占位文件创建到 `os.replace` 之间，若外部进程主动替换占位仍有短暂竞态；应用自身并发不会触发。 | `src/parallel_downloader.py:60`–`:110`。若需要防止不协作的外部进程同时写入，需评估平台级 no-replace rename。 |

## 4. 代码变更明细

### 本轮 Range 与同名下载修复：`9b88ea6`

| 状态 | 完整路径 | 变更摘要 |
|---|---|---|
| 修改 | `/Users/xxd/Playground/video-scrap/src/downloader.py` | 增加任务私有 staging、失败清理、yt-dlp 返回码和合并后路径处理。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | 在播放缓存写入前校验 Range、编码、总长和边界。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/parallel_downloader.py` | 增加 Range 响应/长度校验、独立临时文件和不覆盖发布回退。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_download_name_reservation.py` | 覆盖直链、普通/X、HLS 同名并发、跳号和失败清理。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_media_proxy_ranges.py` | 覆盖错位、短/长响应、压缩、探针和完整 200 回退。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_parallel_downloader.py` | 增加 Range 故障、并发同名、硬链接回退与占位清理测试。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_x_downloader.py` | 适配 staging 目录及通用 post hook，保留 X 选择和最终路径验证。 |

### 前序 X 视频功能：`40b52d0`

| 状态 | 完整路径 | 变更摘要 |
|---|---|---|
| 修改 | `/Users/xxd/Playground/video-scrap/README.md` | 记录 X 链接、浏览器登录态和预览/下载用法。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/scraper.py` | 增加 X 帖子识别、每个视频条目、播放格式和浏览器 Cookie 处理。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/main_window.py` | 增加 X 登录浏览器选择并把会话选择传给抓取/下载。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py` | 为 X 选择可播放格式，通过代理传递播放请求头。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/video_list.py` | 为 X 缩略图请求传递会话 Cookie。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/workers.py` | 在后台抓取和缩略图线程中传递浏览器会话信息。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | 为 X 播放媒体请求转发受限域名 Cookie/请求头。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/downloader.py` | 下载 X 帖子中当前选中的视频。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_x_downloader.py` | X 下载索引、命名、浏览器会话和成品路径测试。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_x_preview.py` | X 播放格式和本机 HTTP 代理集成测试。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_x_scraper.py` | X URL、播放列表索引、Cookie 域名过滤测试。 |

### 前序设置与回归测试基线：`44069f4`

| 状态 | 完整路径 | 变更摘要 |
|---|---|---|
| 新增 | `/Users/xxd/Playground/video-scrap/.gitignore` | 忽略虚拟环境、下载文件和本地运行产物。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/js_decoder.py` | 降低置换数组解码时的重复查找成本。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/settings.py` | 设置写入改为临时文件替换，并处理损坏/异常输入。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_parallel_downloader.py` | 建立直链分段下载基础回归用例。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_config_hls.py` | 补充播放器配置 HLS 解析测试。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_flow.py` | 覆盖抓取流程和网络错误路径。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_settings.py` | 覆盖设置读取、归一化和持久化。 |

### 本文档

| 状态 | 完整路径 | 变更摘要 |
|---|---|---|
| 新增 | `/Users/xxd/Playground/video-scrap/diary.md` | 按本次要求记录项目进度、修复、环境、验证和待办；新建文件，没有追加旧文档。 |

**删除文件：无。**

## 5. 当前环境信息

```text
操作系统：macOS 27.0，Build 26A428
项目虚拟环境 Python：3.11.16
系统 python3：3.9.6（低于 README 的 Python 3.10+ 要求；运行项目应使用 .venv）
ffmpeg：当前执行环境 PATH 中未找到
```

关键依赖版本（从项目 `.venv` 已安装包元数据读取）：

```text
PyQt6==6.11.0
PyQt6-WebEngine==6.11.0
yt-dlp==2026.8.19
requests==2.34.2
beautifulsoup4==4.15.0
lxml==6.1.3
Pillow==12.3.0
pycryptodome==3.23.0
```

启动命令：

```bash
cd /Users/xxd/Playground/video-scrap
source .venv/bin/activate
python main.py
```

运行配置：

- 默认下载目录：`Path.cwd() / "downloads"`，可在 GUI 中更改。
- 媒体代理只监听 `127.0.0.1`，端口绑定 `0`，由操作系统动态分配；没有固定端口。
- macOS 设置文件：`~/Library/Application Support/VideoScraper/settings.json`。
- 播放缓存位于 `video_scraper_cache_*` 临时目录，代理停止时清理；没有独立缓存服务。
- 数据库/Redis：项目代码和 README 未发现相关配置。
- HLS：README 建议安装 ffmpeg；当前执行环境 PATH 中未找到，因此真实 ffmpeg HLS 路径尚未实测。

## 6. 当前项目状态

### 已完成

- 多阶段视频发现：yt-dlp、HTML 媒体标签/元数据、JS 混淆解码和 WebEngine 回退。
- GUI 视频列表、缩略图、内嵌播放器、播放代理和下载队列。
- X 帖子多视频解析、浏览器会话选择、应用内预览和按选中视频下载。
- Range 下载与播放缓存的范围、响应长度和内容编码校验。
- 直链、HLS、普通 yt-dlp、X、ffmpeg 后端的任务暂存隔离与不覆盖发布。
- 最新代码 commit `9b88ea6` 已推送；`main` 与 `origin/main` 同步。

### 进行中

- 没有正在进行的代码修改；提交后的工作区干净。

### 未开始/尚未验收

- 没有从 README 或 Git 中发现正式登记的未开始模块。
- `[未解决]` HLS ffmpeg 真实站点端到端验收尚未完成；目前由离线模拟覆盖。
- `[未解决]` Windows 直接分段下载尚未验证，见下一步计划。

## 7. 待办与下一步计划

1. **P1：验证真实 HLS。**当前没有可发现的 `ffmpeg` 可执行文件。安装或提供其路径后，运行 HLS 播放、完整下载、取消保留 partial 和后备流程；此项需要用户提供/确认可用 ffmpeg 环境。
2. **P1：确认 Windows 支持范围。**当前直接分段实现使用 `os.pwrite`，只在 macOS 测试。若要维持 README 的 Windows 支持，需要可移植定位写入并在 Windows runner 验证。
3. **P1：限制代理资源占用。**为持续 `403` 加退避；为忽略 Range 且无 `Content-Length` 的 `200` 设置缓存上限，验证播放器仍保持流式响应。
4. **P2：补 HLS `EXT-X-BYTERANGE`。**解析字节范围或把不支持的清单交给 yt-dlp，并补有重复媒体 URI 的清单用例。
5. **P2：修抓取准确性。**用重定向最终 URL 解析相对媒体地址；iframe 改用精确 hostname；按 URL path/extension 分类 HLS、DASH。
6. **P2：修 UI/线程边界。**给 JS 渲染回调增加任务代次；转义预览链接；让 WebEngine 拦截器经 Qt signal 更新 UI；改进缩略图和下载的协作式取消。
7. **P3：评估外部进程竞态。**应用内部同名并发已隔离；若需防范不协作的外部程序在发布占位期间替换文件，评估平台级 no-replace rename。

需要用户补充/确认的只有真实 HLS 验收所需的 ffmpeg 路径，以及是否继续把 Windows 作为本项目的正式支持目标；其他待办可直接按优先级处理。

## 8. 信息来源与追溯

本记录依据仓库和本轮实际执行输出，主要命令如下：

```bash
git status --short --branch
git log -8 --date=iso-strict --pretty=format:'%h %ad %s'
git show --stat --oneline --decorate -3
git reflog show origin/main --date=iso -5
rg --files -g '!downloads/**' -g '!.venv/**'
sw_vers
.venv/bin/python --version
.venv/bin/python -m compileall -q src tests
git diff --check
```

测试脚本（均使用项目 `.venv`）：

```bash
.venv/bin/python tests/test_download_name_reservation.py
.venv/bin/python tests/test_media_proxy_ranges.py
.venv/bin/python tests/test_next_data_ev.py
.venv/bin/python tests/test_parallel_downloader.py
.venv/bin/python tests/test_scraper_config_hls.py
.venv/bin/python tests/test_scraper_flow.py
.venv/bin/python tests/test_settings.py
.venv/bin/python tests/test_x_downloader.py
.venv/bin/python tests/test_x_scraper.py
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_x_preview.py
```

交互式 shell 历史不在当前非交互执行环境中；未读取用户私有 `~/.zsh_history`。Git commit/ref 日志、Codex 命令记录和上述工具输出用于追溯。时间统一使用 `Asia/Kuala_Lumpur`（UTC+08:00）。
