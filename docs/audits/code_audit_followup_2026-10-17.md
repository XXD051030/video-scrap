# 代码问题复核与修复记录

> **后续核验更正（2026-10-18 09:42，Asia/Kuala_Lumpur，UTC+08:00）**：本记录中“23 项已逐项处理”的表述过于笼统。已修复的具体场景仍有回归保护，但 HLS 最终回退完整性、逐轨音频覆盖、畸形 Next 配置回退及交互浏览器 X 预览会话等分支仍有遗漏。不能把本轮 378 项检查通过理解为相关功能所有路径均已修完。详见本文第 7 节及 `code_audit_2026-10-18.md`。

## 1. 基准与结论

- 汇总时间：2026-10-17 22:56（Asia/Kuala_Lumpur，UTC+08:00）。
- 项目路径：`/Users/xxd/Playground/video-scrap`。
- 修复前基准：`9f8dd8c5c1b0985a6804136db81c36e5db859e8e`；源码版本 `0.3.2`。
- 本轮范围：用户选中的原审计 B04–B26，共 23 项。B01–B03 已在基准提交修复，本轮保留其回归检查。
- 复核结论：这 23 项确实存在对应的代码缺陷或功能缺口，已逐项处理。B21 是排队任务管理缺口，B22 是浏览器会话传递缺口；并非每项都是每次使用都会触发的故障。
- 去重后 **363 项定向回归 + 15 项源码应用集成检查 = 378 项通过**，无失败、错误或跳过。不是对所有网站、系统或任意 JavaScript 的完整保证。
- 直接 DASH/MPD 已与 HLS 分流并验证下载。**直接 DASH 的应用内预览仍不支持**，现在明确提示，并禁用该媒体的播放入口，避免错误进入 HLS 路径。全屏按钮仍可用。
- 改动保留在工作区；本轮没有提交、推送、改版本、创建 Release 或重打包。
- 原报告 `/Users/xxd/Playground/video-scrap/code_audit_2026-10-17.md` 保留为历史基准；不覆盖旧日记和旧构建记录。

## 2. 逐项复核与处理

表中相对位置均以项目路径为根；行号对应本轮完成时的源码，后续编辑可能移动。

| 编号 | 复核依据与原始原因 | 修复方式及结果 | 主要位置与检查 |
|---|---|---|---|
| B04 | 点击滑条轨道时手工设置 sliderDown，却没有对应移动、释放处理；自动播放进度因此被跳过。 | 完整处理轨道按下、拖动、松开；隐藏、失去鼠标抓取等情况下也释放状态。保留点击现有滑块时的原生行为。 | `src/gui/preview_panel.py:35`；真实 Qt 鼠标事件验证拖动、释放、自动进度恢复和音量滑条。 |
| B05 | 普通 HLS 的无扩展名视频/音轨子清单未按 master 标签标识，被按普通片段转发。 | 普通与包装 HLS 统一根据 STREAM-INF、MEDIA 等标签强制子清单重写。 | `src/media_proxy.py` 的清单重写与 `_wrap_relative`；子清单和内部相对地址回归。 |
| B06 | MPD 被标记 is_hls，XML 被当成 M3U8，预览也没有 DASH 地址重写。 | 新增独立 is_dash 标记；直接 MPD 使用 yt-dlp 下载；代理和预览明确拒绝不支持的直接 DASH 播放。 | `src/scraper.py:108`、`:424`；`src/downloader.py:616`；`src/gui/preview_panel.py:410`；`src/media_proxy.py:974`。真实本地 DASH 下载验证音视频流。 |
| B07 | HLS 原生选流仅选最高带宽，清晰度未传入 fallback。 | 将目标清晰度贯穿原生、yt-dlp、FFmpeg；选择声明高度不超过目标的最佳流，并关联对应音轨。没有可确认的合适高度时明确失败，不静默超出目标。 | `src/downloader.py:1259` 及各 HLS 路由；原生子清单请求、音轨关联和两种 fallback 参数检查。单一媒体清单本身没有多清晰度可选。 |
| B08 | 收尾失败后 fallback 已重新传输，监督器与 GUI 却仍停留 finalizing；首个状态事件还可能被节流。 | 允许显式 finalizing→downloading；状态变化不被 100ms 传输节流吞掉。GUI 收到监督器状态后恢复暂停，仍忽略没有状态切换的过期进度，保留取消保护。 | `src/download_job.py:383`；`src/gui/main_window.py:1376`。状态、暂停恢复、取消优先和旧进度检查。 |
| B09 | concat 文件把单引号错误转义，保存目录含单引号时 FFmpeg 无法打开片段。 | 按 FFmpeg token 语法关闭引号、在引号外转义、再开启；引号内保留反斜杠。 | `src/downloader.py` 的 concat 写入方法。真实 FFmpeg 验证含单引号、空格、反斜杠的路径。Windows 本机未实测。 |
| B10 | fsync 的全部 OSError 被吞掉，实际存储失败仍可能发布成功。 | 仅对明确“不支持同步”的错误允许降级；EIO、ENOSPC、EROFS 等阻止发布，关闭句柄、清理临时文件，并保留既有成品。 | `src/parallel_downloader.py:332`。故障注入验证错误不会变成成功；未制造真实硬盘故障。 |
| B11 | passthrough 转发压缩头与长度，却用 iter_content 发送已解压正文。 | 请求 identity；上游仍压缩时转发原始 wire bytes，使正文与 Content-Encoding/Content-Length 一致。 | `src/media_proxy.py:1695`。gzip 原始字节、编码及长度一致性检查。 |
| B12 | Enter 与工具栏绕过部分忙碌检查；回调使用共享当前 URL，没有任务身份。 | 静态、JS、交互阶段共用忙碌状态；为任务增加代次守卫并捕获来源 URL、登录选择；旧回调无法覆盖新列表或恢复新任务按钮。 | `src/gui/main_window.py:774`、`:783` 及抓取连接。受控 A/B 任务、旧成功/失败回调、Enter/工具栏和来源地址检查。 |
| B13 | 重定向后仍以输入 URL 拼资源，并忽略 HTML base。 | 分别保留用户输入来源、最终文档 URL、有效 base；媒体、封面、iframe 和播放器配置按 base 解析；Referer 使用最终文档地址。 | `src/scraper.py` 的 `_HtmlScanResult`、`_scrape_html` 与配置解析。重定向、相对 base、跨域 base 和无效 base 回归。 |
| B14 | blob/非 HTTP 来源被作为完整直链返回，阻碍正确回退；复核还发现加密 Next 配置绕过校验。 | HTML、隐藏 URL、浏览器结果和加密配置统一校验 HTTP(S)、主机、端口、凭据等；不把 blob 当可下载影片。无效 Next 配置可继续尝试 TanStack。 | `src/scraper.py:348` 及配置解析。含非法 IPv6、越界端口、userinfo、blob 等用例，以及无效 Next→有效 TanStack。 |
| B15 | 网络拦截器把单个 TS/M4S 分片当完整影片，模型构造也接受它们。 | 交互捕获不再收集 TS/M4S；浏览器结果构造再次排除独立分片，保留 manifest 和完整媒体候选。 | `src/gui/interactive_scraper.py` 的媒体扩展列表；`src/scraper.py` 的 `build_items_from_urls`。片段不会进入完整视频列表。合法独立完整 TS 的专门支持未新增。 |
| B16 | preview.mp4 名称规则全站无条件过滤，可能删掉唯一真实影片。 | 普通 preview 文件保留；只有识别到对应播放器主视频配置时，才应用 teaser 过滤。 | `src/scraper.py` 的候选及配置处理。唯一 preview、preview 与无关媒体并存、已识别主播放器检查。 |
| B17 | Python replace 默认全替换；附近其他变量操作、原始候选与无序集合也可能生成错误 URL。独立复核还发现字符串文本和属性赋值被误识别。 | 跟踪受支持的同变量操作链；字符串搜索只替换首个匹配，处理 JS 替换字符串 token，稳定返回最终结果。用保持偏移的词法遮罩忽略字符串/注释中的示例赋值，排除 window.url 等属性赋值；歧义/不支持操作交给浏览器回退。 | `src/js_decoder.py:208`。15 项语义回归；不执行任意 JS，也不声称支持完整 JS 语法。 |
| B18 | 设置加载未捕获 UnicodeDecodeError；进一步验证发现 JSON 超长整数/深层嵌套也可抛异常。 | 编码、JSON 值/结构及解析限制错误进入统一恢复路径；保留唯一损坏文件副本，使用默认值，日志显示原因和备份路径。备份失败也会报告。 | `src/settings.py:63`、`:117`。损坏 UTF-8、5000 位数字、深层 JSON、备份保留与后续保存检查。 |
| B19 | 启动、下载和打开目录直接 mkdir，失败外泄。 | 启动失败记日志并保持窗口可用；下载、选择和打开目录统一捕获错误，说明路径和原因，可重选目录重试。实际文件写入失败仍由下载错误处理报告。 | `src/gui/main_window.py` 的初始化和 `:1149`。同名文件占位、操作失败、重选目录恢复检查。 |
| B20 | embed 主机用子串匹配，placeholder 主机没有点边界。 | 使用 hostname 精确相等或带点的子域匹配，避免 youtube.com.evil 等误匹配；保留既有合法 player/embed 路径识别。 | `src/scraper.py:184` 和 placeholder 判断。假域名、真实子域和合法播放器路径回归。 |
| B21 | 第三个及以后的任务只在内部队列，开始前没有单独移除入口。 | 下载区域新增 Queued downloads 管理菜单，逐项显示任务并可 Remove；只移除仍在队列的任务，不取消其他活动任务。 | `src/gui/main_window.py:1271`、`:1285`。两活动任务暂停时，逐个移除队列且不启动额外 worker。 |
| B22 | 普通交互浏览器只交回 URL，没有把登录 Cookie 带到预览与隔离下载进程。 | 观察共享 profile 的 Cookie 添加/删除，导出相关主机的作用域快照；VideoItem/子进程保留结构；requests 每次请求及跳转按 CookieJar 限制发送；yt-dlp 使用保留严格策略的后端。认证 HLS 经任务私有代理，FFmpeg 只访问回环地址，不接收全域 Cookie 头。 | 新增 `src/browser_cookies.py`；浏览器、预览、代理、下载与任务序列化。真实 Qt host-only/domain 区分；domain/path/secure/expiry；真实本地认证 MP4/HLS/DASH 及跨主机跳转检查。不保证依赖 localStorage、其他令牌或第三方风控的登录站点。 |
| B23 | 隐藏弹窗/完成信号未释放旧页面，profile 又由短生命周期对象拥有。 | profile 改由应用拥有；完成、失败、超时、取消、关窗时停止并 deleteLater 页面/视图，停止计时器；过期回调忽略。主窗口清理对应 renderer/dialog，关闭时使抓取代次失效。 | `src/gui/interactive_scraper.py:99`、`:360`；`src/gui/js_renderer.py:168`；主窗口收尾。7 项独立生命周期/设置检查，加真实 Chromium CookieStore 检查；未量测真实站点长期 RAM 增量。 |
| B24 | bytes=8-3 等非法请求可以产生负 Content-Length。 | 解析并验证单 Range 语法、顺序、suffix 和可满足性；非法/反转/多范围/零 suffix 返回 416、空正文及合法长度。 | `src/media_proxy.py:223` 和 `_serve_range`。错误请求、正常范围与原有字节保护回归。 |
| B25 | 大小未知时 suffix Range 没传给上游，退化成整文件请求。 | 无法计算范围时保留合法原 Range，交给 passthrough 转发。 | `src/media_proxy.py` 的 `_serve_range`。未知大小 bytes=-N 的上游请求与响应检查。 |
| B26 | 保存异常被吞掉，Preferences 仍关闭，重启后丢失设置。 | 保存返回结果并保留错误；会话内仍应用设置，失败时弹窗保持打开，说明未落盘并可重试；主题切换保存失败也记日志、状态提示。 | `src/settings.py:88`、`:154`；`src/gui/settings_dialog.py:177`。原文件不变、失败提示、再次 Save 成功、正常保存兼容检查。 |

## 3. 复核中另外收紧的同路径边界

- renderer 构造后的同步加载异常、worker 构造/启动异常现在能解除抓取忙碌状态，避免按钮永远锁住；各有回归。
- 浏览器 Cookie 快照忽略畸形 URL，避免无效页面候选令回调抛异常；不创建无主机作用域 Cookie。
- About 与 Preferences 结束后也安排释放，避免反复打开保留 Qt 子对象。
- 长路径布局测试原先使用单级超过文件系统限制的目录名。新目录校验会正确弹出错误提示，测试因此等待用户输入。夹具改用有效的多级长路径，保留原有路径省略及布局断言；5 项布局检查随后通过。

## 4. 验证清单与证据

### 4.1 去重统计

| 分组 | 通过数量 | 内容 |
|---|---:|---|
| 下载与任务 | 160 | 并行下载、暂停/取消、子进程、同名发布、音频、HLS Range/完整性/分离音轨、包装 HLS、X，以及新增下载审计回归。 |
| 代理 | 53 | Range 13、协议/会话 15、HLS master 10、包装 HLS 9、缓存句柄 6。 |
| GUI 与设置 | 87 | GUI 审计 11、设置 6、Preferences 10、播放器控制 9、下载控制 15、折叠布局 10、界面布局 5、预览选流 10、X 预览 3、真实 CookieStore 1、生命周期 7。 |
| 抓取与 JS | 63 | 地址校验 17、JS 15、抓取流程 4、配置 HLS 4、Next 4、X 6、TanStack 13。 |
| 源码应用集成 | 15 | 依赖、主窗、主题、设置/About、空全屏、合成媒体、直链/HLS/音频下载、Qt 播放、WebEngine、下载任务控制等。 |
| **合计** | **378** | **363 项定向回归 + 15 项集成检查；无失败、错误、跳过。** |

下载批次原始计数为 188，含上表 GUI 的下载控制 15 项及代理 Range 13 项；表中已去重。检查还包括 63 个 Python 源文件的 AST 解析和 `git diff --check`。

### 4.2 可追溯报告

以下文件在忽略的构建目录中，不会自动成为提交内容：

```text
/Users/xxd/Playground/video-scrap/build/audit-recheck-verification.json
/Users/xxd/Playground/video-scrap/build/audit-recheck-download-tests.json
/Users/xxd/Playground/video-scrap/build/audit-recheck-download-tests.log
/Users/xxd/Playground/video-scrap/build/audit-recheck-proxy-tests.json
/Users/xxd/Playground/video-scrap/build/audit-recheck-proxy-tests.log
/Users/xxd/Playground/video-scrap/build/audit-recheck-scraping-tests.json
/Users/xxd/Playground/video-scrap/build/audit-recheck-scraping-tests.log
/Users/xxd/Playground/video-scrap/build/audit-recheck-gui-tests.json
/Users/xxd/Playground/video-scrap/build/audit-recheck-final-source-smoke.json
/Users/xxd/Playground/video-scrap/build/audit-recheck-final-source-smoke.log
```

汇总 JSON 记录基准提交、去重套件数量和本轮源码/测试 SHA-256。分组 JSON 保留真实执行数量及限制；日志保留每项结果。早期沙盒无法绑定本机端口的测试在授权后重跑通过，不把环境拒绝算成产品缺陷或通过结果。执行额度曾中断，续跑后完成，没有遗留测试阻塞。

示例复查命令（使用现有构建环境，不会打包）：

```bash
cd /Users/xxd/Playground/video-scrap
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python tests/test_gui_audit_regressions.py
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python tests/test_gui_lifecycle_regressions.py
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python tests/test_download_audit_regressions.py
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python tests/test_proxy_protocol_regressions.py
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python tests/test_scraper_validation.py
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python tests/test_js_decoder_semantics.py
PATH="$PWD/build/app-assets/bin:$PATH" build/.venv/bin/python main.py \
  --smoke-test "$PWD/build/audit-recheck-manual-smoke.json"
```

### 4.3 已验证环境与限制

- macOS arm64；Python `3.12.14`；PyQt6 `6.11.0`；yt-dlp `2026.8.19`；requests `2.34.2`；psutil `7.2.2`。
- 定向 GUI 用离屏 Qt；最终源码集成用真实 Cocoa/Qt Multimedia 与 WebEngine。
- 下载测试使用本地受控 HTTP、合成媒体和现有打包工具目录的真实 FFmpeg。
- 没有访问真实登录/成人视频站点，没有读取真实浏览器 Cookie；认证测试用合成 Cookie。
- Windows 本机、最低依赖版本组合和新打包二进制未验证。现有 `.app`/ZIP 不会因源码修改自动包含这些修复。

## 5. 文件变更明细

### 5.1 生产代码：修改 12 个，新增 1 个

| 操作 | 完整路径 | 摘要 |
|---|---|---|
| 新增 | `/Users/xxd/Playground/video-scrap/src/browser_cookies.py` | 浏览器快照转严格 CookieJar；每次请求与跳转重新检查作用域。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/scraper.py` | DASH 分类、HTTP 地址验证、重定向/base/Referer、分片排除、preview/embed 规则及会话字段。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/js_decoder.py` | 同变量操作链、JS 字符串替换语义、稳定候选及字面量/属性赋值过滤。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/downloader.py` | 清晰度贯穿 HLS 后端、concat 路径转义、DASH 路由与登录会话使用。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/download_job.py` | 序列化新字段，恢复 fallback 下载状态，避免状态事件被节流。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/parallel_downloader.py` | 存储同步错误阻止发布；使用作用域受限的 HTTP 会话。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | 子清单识别、压缩转发、Range 验证、会话限制与明确拒绝 DASH 预览。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/settings.py` | 损坏配置备份/恢复、解析异常保护及可追溯保存结果。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/main_window.py` | 任务身份与忙碌守卫、异常恢复、排队移除、目录恢复、会话传递及临时对象释放。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py` | 轨道点击拖动释放、会话代理参数、明确 DASH 播放限制。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py` | 应用拥有共享 profile、导出相关 Cookie、过滤分片并释放页面。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py` | 最终页面/会话快照、计时器及页面收尾，忽略结束后的回调。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/settings_dialog.py` | 保存失败说明、保留会话设置并允许重试。 |

### 5.2 测试：修改 4 个，新增 7 个

| 操作 | 完整路径 | 摘要 |
|---|---|---|
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_preferences_dialog.py` | 真实按钮验证保存失败保持弹窗、原文件保护和重试。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_settings.py` | 损坏编码/解析限制备份恢复、会话应用和保存错误返回。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_flow.py` | 重定向用最终文档 Referer 的正确预期。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_ui_layout.py` | 使用有效多级长路径保留布局检查，适配真实目录校验。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_download_audit_regressions.py` | 选流、fallback、路径、磁盘错误、认证媒体和重定向。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_proxy_protocol_regressions.py` | 无扩展名清单、压缩、非法/未知 Range 与 Cookie 作用域。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_gui_audit_regressions.py` | 真实鼠标、抓取代次、同步异常、目录恢复、队列移除和 DASH 提示。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_gui_lifecycle_regressions.py` | 合成页面/profile 配合真实 Qt 验证停止、删除、旧回调和设置重试。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_browser_cookie_export.py` | 真实 Chromium CookieStore 验证 host-only 与 domain 的导出区分。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_validation.py` | 地址/base/协议/候选校验及加密配置回退。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_js_decoder_semantics.py` | 受支持 JS 操作链及错误候选拒绝。 |

本记录为新增文件 `/Users/xxd/Playground/video-scrap/code_audit_followup_2026-10-17.md`。没有删除项目文件。原有未跟踪的 assets、历史报告和日记均保留。

## 6. 当前状态与下一步

- **已完成**：选定 B04–B26 的复核、对应修复/明确功能边界、独立复核、定向回归和源码集成验证。
- **进行中**：无阻塞中的修复任务。
- **本轮未开始**：直接 DASH 预览实现、Windows 本机验证、重新打包、提交/推送及原报告剩余 R/M 风险的治理。
- 原报告的累计缓存额度、异常退出残留、预取退避、缩略图线程退出等条件风险不属于本次选定清单，不能由本轮通过结果推断为已解决。
- 后续如用户要求提交/推送，应提供详细提交正文，并按项目约定把下一批小更新版本递增为 `0.3.3`；本轮源码暂保持 `0.3.2`。
- 如需交付安装包，应先编译包含这些源码的新版，再做打包后检查；当前旧二进制不能用于验收本轮修复。


## 7. 后续核验更正

- 核验时间：2026-10-18 09:42（Asia/Kuala_Lumpur，UTC+08:00）。本节追加事实更正，前文保留上轮执行历史。
- 已读取关联审查会话 `01a14ac7-d700-7431-b098-df8c831b846a`，并核对其报告、源码清单、复现脚本及当前代码。
- 上轮记录的 24 个源码/测试文件 SHA-256，与新审查清单及当前磁盘一致。因此新问题是在包含上轮修复的工作区中发现的，不能归因于尚未提交或审查了旧安装包。
- 以下是上轮验收没有覆盖的具体缺口；本次仅对照和复现，尚未修改业务代码来补修这些缺口：

| 新报告编号 | 当前已确认情况 | 上轮结论/测试的边界 |
|---|---|---|
| D01 | 原生和 yt-dlp 失败后的最终 FFmpeg 回退仍可接受缺片输出。复跑已有离线脚本：预期 3 秒，成品 2.03 秒，状态 completed。 | 原生空片拒绝和原生合并短文件检查有效；普通单轨最终回退没有统一执行期望时长检查。 |
| D02 | 存在音视频流且容器总时长足够，不能证明音轨完整。复跑：视频 3.000000 秒、音频 2.005333 秒，预期音频约 3.03 秒，仍 completed。 | 原检查覆盖完全缺少音轨及容器整体截短，没有逐轨时间覆盖验收。 |
| D19 | Next 根为数组、props 为字符串或 pageProps 为数字时，未检查类型的 get 抛异常；同一 try 内的 TanStack 被跳过。三个离线输入均返回零结果。 | 上轮修复“无效 URL 配置可回退”，没有覆盖“JSON 合法但嵌套结构错误”。前文对无效配置回退的概括应收窄。 |
| D16 | X 来源优先走 formats 选择，浏览器构造的直链 formats 为空会禁用 Play；X 代理分支没有交付 browser_cookies。 | 正常 X 提取器及普通站点 Cookie 路径的验证，未覆盖 Qt 交互浏览器→X 直链→预览的组合。真实登录网站失败未实测，路由和参数遗漏由组件流程/源码确认。 |

- 新报告还包含此前明确未开始治理的 R/M 项，例如资源读取上限、缓存总量、失败退避、旧缩略图信号、线程强停、设置并发和敏感头来源边界。这些不是已选 B04–B26 全部重新失效，也不能据此抹去新的复现证据。
- 另有新发现的既有缺陷，例如直链 HTTP 200 HTML/空文件被当视频、异常高度值、HLS BYTERANGE 预取预算、普通清单接收登录 HTML、发布占位被第三方替换的窄竞态。不能仅因它们出现在改过的文件，就认定由本次改动引入。
- 新审查明确认可的既有修复包括：滑条状态、抓取代次、重定向/base、blob/独立分片过滤、preview 文件名、Cookie 域/路径/Secure/expiry、设置损坏恢复和保存失败提示、JS/交互页面释放等。
- 测试通过仅说明已执行断言满足。具体反例：旧代理测试明确允许 Range 被忽略后缓存整个小夹具；BYTERANGE 测试只确认标签保留，没有验证预取请求区间。增加通过数量不能替代补齐验收契约和失败分支。
- 当前应表述为：**已有多项具体修复且原有回归仍通过，但仍有未闭合的修复分支、此前待处理风险和新发现缺陷，不能宣布全部修完。**
