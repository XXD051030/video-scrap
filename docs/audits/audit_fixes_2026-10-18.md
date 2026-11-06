# 2026-10-18 审查问题修复与验证记录

## 1. 范围与结论

项目：`/Users/xxd/Playground/video-scrap`。对照聊天“审查整个代码库”（`01a14ac7-d700-7431-b098-df8c831b846a`）及本地 `code_audit_2026-10-18.md`，以提交 `63194eafd892734259f2a069d76b042693ea090c` 后的实际源码为基线。

原报告列出的 D01–D23 已逐项核实并实施修复。条件性风险 R01–R12 和维护建议 M01–M07 的处理范围及限制分别列在下文，不能将“实现了防护”“本地复现通过”“Windows 本机通过”混为一谈。

本轮修改了下载、代理、抓取、界面生命周期及测试/打包工具。未提交、推送或更新版本号；源码仍为 **0.3.3**。未重新生成 `.app`、Windows EXE、ZIP、Git 标签或 Release。旧安装包不包含本轮修改。

## 2. 原报告缺陷逐项处理

以下文件均位于 `/Users/xxd/Playground/video-scrap`；完整文件变更路径见第 7 节。

| 编号 | 原现象与根因 | 实施的修复 | 位置及主要验证 |
| --- | --- | --- | --- |
| D01 | 普通 HLS 最终回退只依赖 FFmpeg 成功退出，缺片输出可能发布 | 所有 HLS 后端回退继承已选择清单的期望时长；完成后验证媒体；FFmpeg 增加错误中止选项 | `src/downloader.py`：`_download_to_stage`、`_verify_hls_output`；`tests/test_download_audit_oct18.py` 真实缺首/中/尾片夹具 |
| D02 | 容器最大时长掩盖音轨提前结束 | 分别扫描所选音视频轨道的包时间范围和区间并集，不能靠重复时间戳填补缺口；拒绝覆盖不足的成品 | `src/downloader.py`：`_verify_hls_track`；真实长视频/短音轨、正常双轨回退测试 |
| D03 | 直链的空文件或 HTTP 200 登录页被发布 | 媒体验收层拒绝空文件、明确文本/HTML/JSON/XML/图片响应和相应错误正文；底层任意字节传输测试继续保留 | `src/downloader.py`：`_validate_direct_media`；`tests/test_download_audit_oct18.py` |
| D04 | 同 URL 不同版本的合法 Range 可混入成品或缓存 | 并行分段使用强 ETag 与 If-Match；验证器变化拒绝合成。无强验证器的普通下载使用单个完整响应；HLS 逻辑区间从一次私有完整对象落盘后切片。代理不拼接无验证器的独立 Range，实体检查与缓存写入互斥 | `src/parallel_downloader.py`、`src/downloader.py`、`src/media_proxy.py`；变更/丢失/弱 ETag、无验证器双轨 HLS 与缓存测试 |
| D05 | 自定义敏感头跨协议、端口或重定向传播 | 以协议、规范主机、有效端口比较来源；每次重定向按公共头白名单剥离其他头；Cookie 仍按自身作用域处理 | `src/browser_cookies.py`：`scoped_headers`、`ScopedCookieSession.rebuild_auth`；代理/真实 requests 重定向夹具 |
| D06 | 未转义来源 URL 注入 QLabel 链接属性 | 分别转义属性与显示文本，关闭自动打开；点击后仅允许无嵌入凭据的有效 HTTP(S) URL | `src/gui/preview_panel.py`：`_render_url_label`、`_open_source_link`；Qt 富文本和协议门禁测试 |
| D07 | 快速失败的预取无退避、反复重排 | 明确报告预取进展；增加可取消指数退避、Retry-After、失败提示与有界在途任务 | `src/media_proxy.py`：`_Prefetcher`、`_HlsPrefetcher`；快速 403/失败计数及退避测试 |
| D08 | 发现视频直链时把整个媒体当 HTML 读取 | 已知媒体 URL 直接形成条目；扩展名不明确但响应类型为媒体时不读正文；HTML 改为有字节/时间预算的流式读取，取消可关闭正在等待响应头或正文的请求 | `src/scraper.py`：`scrape`、`_scrape_html`；`tests/test_audit_core_regressions.py`、`tests/test_scraper_network_boundaries.py` |
| D09 | 清单、密钥、普通 HLS 分片无输入预算；片段 chunks+join 放大内存 | 普通/AES 片段流式写私有文件；清单、密钥、片段独立限额；共享响应看门狗可中止阻塞读；禁止超限正文继续累积 | `src/http_limits.py`、`src/downloader.py`、`src/media_proxy.py`；流式内存、AES、超限、慢头/慢正文测试 |
| D10 | 缩略图全量读取并在界面线程解码，无尺寸或总时限 | 使用工作线程内 Qt 网络和解码；限制传输、像素、尺寸及时间；只向界面交付缩放后的 PNG | `src/gui/workers.py`：`ThumbnailWorker`、`decode_thumbnail`；真实慢头、慢正文、取消、缩放测试 |
| D11 | 短 Range 预取遇 200 下载整部影片 | 忽略 Range 的响应也在目标区间结束后停止；过大的跳转回退不消费整部文件 | `src/media_proxy.py`：`_Prefetcher`；忽略 Range 夹具检查消费字节和缓存范围 |
| D12 | BYTERANGE 预取丢失每个逻辑片段的范围 | 清单计划同时保存资源、起点、长度；按实际范围请求并去重在途工作 | `src/media_proxy.py`：`_rewrite_m3u8`、`_HlsPrefetcher`；共享对象不同逻辑片段测试 |
| D13 | 直播旧条目/缓存一直保留，注册也可竞态重复 | 共享缓存字节预算和剩余空间检查；直播窗口淘汰及有限宽限；活动请求受保护；子资源查找/注册原子化 | `src/media_proxy.py`：`_CacheBudget`、`_retire_manifest_children`、`_wrap_relative`；100 次滑动刷新、并发注册及预算测试 |
| D14 | GUI 等待、强停网络 QThread，可能遗留锁 | 取消改为协作请求；切换立即使旧结果失效；运行线程脱离控件父对象并保留到 finished 后删除；GUI 关闭采用重试，事件循环退出后继续排空线程 | `src/gui/workers.py`、`video_list.py`、`preview_panel.py`、`main_window.py`、`main.py`；持锁工作线程、销毁、快速切换与退出测试 |
| D15 | 提取器传回 Infinity 高度后 int 转换异常 | VideoItem 边界规范化非有限/无效数值并复制格式数据；预览选流再防御检查 | `src/scraper.py`：`VideoItem.__post_init__`；`src/gui/preview_panel.py`：`_format_height`；无效元数据与选流测试 |
| D16 | X 浏览器直链先走空 formats 分支，Play 禁用；浏览器 Cookie 漏传 | 先识别直接媒体，再走 X 格式选流；代理统一传入浏览器 Cookie | `src/gui/preview_panel.py`：`show_video`、代理注册；离线 X 直链和 Cookie 测试 |
| D17 | 已排队的旧缩略图覆盖新选择 | 选择、清空、停止增加代次；回调同时检查代次与控件存活 | `src/gui/preview_panel.py`：缩略图调度/回调；旧 ready/failed 排队信号测试 |
| D18 | 已结束的缩略图 QObject 累积 | finished 后移出跟踪集合并 deleteLater；运行中的对象由独立注册表保留 | `src/gui/workers.py`、`src/gui/video_list.py`、`src/gui/preview_panel.py`；Qt DeferredDelete 对象数量与快速替换测试 |
| D19 | 合法 JSON 的错误嵌套类型阻断 TanStack | Next.js 逐层检查映射类型；Next 与 TanStack 独立容错和回退 | `src/scraper.py`：`_extract_next_data_ev_hls`、`scrape`；根数组、props 字符串、pageProps 整数后仍恢复有效 TanStack |
| D20 | HLS 预取锚点只增不减 | 采用最新播放请求及绝对序列，直播刷新重映射；HEAD 和预取不作为播放推进 | `src/media_proxy.py`：`_note_player_segment`；从第 10 片退回第 1 片及范围/序列测试 |
| D21 | HTTP 200 HTML 被当作成功 HLS 改写 | 不信任扩展名/类型；核对 EXTM3U、URI 所需的 EXTINF/STREAM-INF、悬空标签和资源数量；错误正文返回可解释失败 | `src/media_proxy.py`：清单读取、`_rewrite_m3u8`；HTML、仅标记加错误文字、悬空结构测试 |
| D22 | SettingsStore 保存及通知在锁外乱序 | 持久化和内存提交序列化；通知按提交顺序排队，在数据/事务锁外单独派发，支持回调再入 | `src/settings.py`；确定性交错、落盘值、回调顺序和再入测试 |
| D23 | 占位文件后 os.replace 可覆盖第三方替换目标 | 删除占位替换方案；采用硬链接或系统原子禁止覆盖重命名，不支持时明确失败 | `src/parallel_downloader.py`：`_publish_unique`、`_rename_without_replacement`；第三方抢占、同名并发和已有文件保护测试 |

### 验证中额外发现并修复的问题

1. **普通 HTTP/1.0 正文被提前截断**：新连接管理最初在连接正常脱离响应时主动 shutdown，截断了仍归响应所有的正文。改为正常关闭不强停套接字，主动取消路径才 shutdown；增加真实 HTTP/1.0 回归，下载及音频/HLS 测试重跑通过。
2. **Qt 缩略图重定向复用已准备好的 Cookie**：尽管 CookieJar 查询规则正确，自动重定向仍将前一跳 Cookie 发到另一主机。改为有限次数的手动重定向，每一跳创建新请求并重新取适用 Cookie；跨主机、跨路径、Secure、过期测试通过。
3. **无强 ETag 的单路下载没有百分比**：不信任探测长度后，实际完整响应的 Content-Length 未更新进度总量。现在采用当前完整响应的长度；正常百分比和实际 UI 暂停/恢复/取消检查通过。
4. **并发清单刷新混用计划**：不同响应可分别写入条目和区间/序列。完整清单计划现在在同一把锁内发布，预取与播放请求也在该锁下取一致快照；普通/包装清单均有确定性交错回归。
5. **临时 429/503 永久废弃有效缓存**：错误页的 ETag 不代表媒体实体。非 200/206 响应不绑定或否定媒体版本；明确的 412 仍使已绑定实体失效；429/503 后恢复 206、错误页不首次绑定均已测试。
6. **清理标记非有限创建时间误判**：NaN/Infinity 等所有者时间不能证明已失活。改为保守保留损坏标记，增加活跃目录不误删测试。
7. **统一测试入口漏传 tmp_path**：首次合并运行有 4 项 X 下载测试因夹具参数缺失报错。增加显式临时目录支持及入口自测；不支持的夹具仍明确报错，不把导入成功算作测试通过。

8. **已完成响应的延迟取消**：看门狗的取消判断可在正文迭代结束后才返回，进而中止已经结束的响应；还可能影响已经释放回池的连接。增加完成状态互斥/重查，已完成的 raw 响应不再 shutdown 套接字；两个确定性交错回归及真实慢响应验证通过。

## 3. 条件性风险的处理与保留边界

| 编号 | 本轮结果 | 尚需明确的边界 |
| --- | --- | --- |
| R01 | 预取使用代次、在途去重和有界池，不建立无限旧任务队列 | 不代表整个应用所有网络请求合计最多 8 或 16；下载进程、缩略图、预览各有自己的限制 |
| R02 | 每个代理拥有预取池、活动响应及连接；停止主动中止读；响应头/正文有看门狗；池线程不阻止解释器退出 | 已在本机真实慢头/慢正文和独立进程测试；系统 DNS/内核 I/O 的所有平台行为没有穷尽证明 |
| R03 | 新缓存/任务目录记录 PID、进程创建时间、所属组；后台只回收超过一天且所有者确定失活的标记目录 | **保留**：无标记历史目录、损坏标记、符号链接、活跃/无法确认失活的目录；本轮不会盲删旧缓存或其他进程文件 |
| R04 | 后端执行前建立所有权握手；POSIX 私有进程组、Windows kill-on-close Job Object 负责后代归属 | 本机测试覆盖父进程先退出、尚未枚举的后代仍被终止；**Windows Job Object 的本轮原生结果待验证** |
| R05 | 下载状态在锁内提交并排队，单派发者在锁外按顺序通知 | 外部 GUI 仍通过 Qt 信号接收；回调执行线程不能假定为主线程 |
| R06 | 文件名处理 Windows 保留名、尾部点/空格、UTF-8 字节预留；显示工具处理非有限数值 | **待验证**：Windows 实际文件系统、超长总路径、网络共享；不能只靠文件名函数保证任何目录路径均可写入 |
| R07 | 解码器改为相邻声明查找；加入输入、声明数量、数组元素、候选长度预算 | 超出静态解码能力返回空结果，交由既有浏览器回退；不是完整 JavaScript 解释器 |
| R08 | JSPageScraper 每次调用独立代次、页面和定时器回调 | 已覆盖同实例 A→B 后旧回调到达，不以当前 UI 不复用为理由忽略接口缺口 |
| R09 | 默认日志隐藏 URL 用户信息、查询、片段及已知命名路径签名 | 不能保证识别任意网站在无标识路径中自定义的秘密；没有增加日志自动上传 |
| R10 | SettingsStore 订阅去重、返回幂等取消句柄；代理停止解除订阅并拒绝停止后的配置/状态通知 | 已经进入执行的外部回调不能被撤回；调用方需使用生命周期保护 |
| R11 | 删除 Session 普遍线程安全的绝对说明；明确连接池并发与 Session 配置约束；缩略图改为每工作线程独立 Qt 网络管理器 | 这属于并发接口契约修正，没有伪称复现并修好了原报告未证明的 CookieJar 竞态 |
| R12 | 根据实际 WebEngine availability 禁用/隐藏不可用入口 | 正常发行依赖仍包含 WebEngine；缺依赖降级路径有模拟验证 |

### 3.1 仍需观察的测试现象

**[待观察，未证明根因]** 一次重复源码集成检查中，HLS 视频子清单返回 502，只有音轨继续。原记录保存在 `build/audit-fixes-018/source-smoke-verified.json` 和同名日志。后续诊断运行全部通过；又修复了可确定复现的延迟取消竞态并增加清单错误日志。最终冻结源码的 15 项应用检查、32 项代理边界检查均通过，15 次 HLS 双轨切换压力检查也通过。**没有将该次 502 直接归因于看门狗，也没有证明它永远不会再出现。** 若复现，新增的清单错误日志可继续定位。

## 4. 工程维护建议处理

| 编号 | 结果 |
| --- | --- |
| M01 | 新增 `requirements-release-macos-arm64.txt`，记录本地 CPython 3.12.14 环境的全部已安装分发版本；没有宣称已有 Windows 锁定环境或 wheel 哈希级可重现构建 |
| M02 | 新增 `scripts/run_tests.py`，每个测试模块独立进程，执行 unittest/函数测试与显式 tmp_path 夹具，保存实际执行数、失败、跳过及超时日志 |
| M03 | 新增 GitHub Actions macOS/Windows 检查；手动 package 选项可构建并检查成品；统一 README/Windows 文档验证范围。**工作流尚未推送/运行，Windows 和新成品结果仍待验证** |
| M04 | macOS 构建报告允许缺 Git 或 .git，成品生成不再因可选 Git 元数据失败 |
| M05 | Windows spec 从 `src/__init__.py` 生成 VERSIONINFO，文件属性版本与 About 同源；资源生成已测试，**Windows EXE 属性实测待验证** |
| M06 | 删除 MainWindow 无效的全局 lane limiter 字段与传参，说明下载工作者按独立进程限制并发；不再误示全应用共享 16 路 |
| M07 | 将输入预算、进程归属、临时目录归属等公共规则提取为独立模块，后端共享验收契约；**没有进行大规模文件拆分或宣称重构已完成**，避免为行数重写正常流程 |

## 5. 明确的预算和行为变化

- HTML：最多 8 MiB；已知媒体 URL 或媒体 Content-Type 不作为 HTML 正文扫描。
- HLS 清单：2 MiB；普通/解密片段：64 MiB；AES-128 密钥：16 字节；HLS 无强验证器的范围对象完整暂存：每对象 512 MiB；片段/范围对象读取期限 120 秒。
- 代理缓存：共享 512 MiB；单条目 64 MiB；分块记账并检查保留可用磁盘空间；达到预算后放弃额外缓存，不能把缓冲秒数理解为永久无限缓存容量。
- 直播旧资源：最多保留 32 个过期条目的短宽限，常规宽限 10 秒；活动播放器请求受保护。
- HLS 预取：每代理最多 8 个任务，不建立无限队列；失败可退避，鉴权失效建议重新抓取。
- 缩略图：8 MiB、1600 万像素、最长边 8192，交付尺寸不超过 1280×720；15 秒传输总限时、3 秒无数据超时、最多 5 次重定向。
- HLS 包时间验收包含 0.5 秒的编码启动/分段舍入容差，不是逐帧精确证明；普通直接文件的 MIME/正文验收也不是任意格式的完整解码保证。
- 无强 ETag 的普通文件使用单路传输，避免把不同版本拼接；无强 ETag 的 HLS BYTERANGE 使用单个对象暂存。资源超出预算或无法原子安全发布时明确失败。

## 6. 验证方式、结果与证据

所有新增网络测试使用本地服务和合成凭据/媒体。没有读取真实浏览器登录信息，也没有依赖真实 X 或外部影片站点作本轮验收。

```bash
cd /Users/xxd/Playground/video-scrap
build/.venv/bin/python scripts/run_tests.py

PATH="$PWD/build/app-assets/bin:$PATH" \
  build/.venv/bin/python main.py --smoke-test build/audit-fixes-018/source-smoke-verified.json
```

验证记录使用完整隔离运行加最后改动对应的定向重跑，合并时按测试文件替换结果，不重复累计用例。最终汇总、应用集成结果与源码哈希见下方生成的验收摘要。

真实应用检查运行于 macOS Cocoa，包含实际帧播放、全屏/暂停、HLS 主清单双轨和 PNG 包装流、音频原格式/MP3/M4A、WebEngine、独立任务暂停/恢复/取消及私有目录清理；`frozen=false`，不能替代已打包 App 验收。

参考的主文档：[Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)、[GitHub Python Actions](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)。实现是否通过以本地测试证据及未来 Windows 原生结果为准。

## 7. 本轮文件变更清单

只记录本轮新增/修改；此前已有的未跟踪图标、旧报告和日记均保留，没有删除。

| 类型 | 完整路径 | 摘要 |
| --- | --- | --- |
| 新增 | `/Users/xxd/Playground/video-scrap/.github/workflows/tests.yml` | macOS/Windows 回归与可选原生打包检查工作流。 |
| 修改 | `/Users/xxd/Playground/video-scrap/README.md` | 记录统一测试入口、版本快照与跨平台验证范围。 |
| 修改 | `/Users/xxd/Playground/video-scrap/WINDOWS_BUILD.md` | 区分早期用户验收与本轮尚待 Windows 原生验证的状态。 |
| 修改 | `/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper-Windows.spec` | 生成并嵌入与源码同源的 Windows 版本资源。 |
| 新增 | `/Users/xxd/Playground/video-scrap/audit_fixes_2026-10-18.md` | 本轮缺陷编号、实现、证据和未验证边界的可追溯记录。 |
| 修改 | `/Users/xxd/Playground/video-scrap/main.py` | 退出后排空保留的工作线程，后台安排失活任务目录清理。 |
| 新增 | `/Users/xxd/Playground/video-scrap/requirements-release-macos-arm64.txt` | 当前本地 Python 3.12 构建环境的完整分发版本快照。 |
| 修改 | `/Users/xxd/Playground/video-scrap/scripts/build_macos.py` | Git 元数据不可用时仍可完成构建报告。 |
| 新增 | `/Users/xxd/Playground/video-scrap/scripts/run_tests.py` | 统一独立进程执行测试、临时目录夹具、真实计数和结果日志。 |
| 修改 | `/Users/xxd/Playground/video-scrap/scripts/smoke_app.py` | 集成检查失败时记录遮蔽凭据后的应用日志和最近本机请求，便于追溯偶发播放错误。 |
| 新增 | `/Users/xxd/Playground/video-scrap/scripts/windows_version.py` | 安全读取源码版本并生成 Windows VERSIONINFO。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/browser_cookies.py` | 跨来源头与重定向作用域、可取消连接及响应头期限。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/download_job.py` | 状态按提交顺序通知、进程所有权握手与有标记暂存。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/downloader.py` | 逐轨 HLS 验收、输入预算、流式片段及单对象范围暂存、直链错误正文拒绝。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py` | 默认隐藏媒体 URL 的日志凭据信息。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py` | 每次运行独立代次、页面与定时器回调。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/main_window.py` | 实际浏览器能力判断、协作关闭、日志隐藏及删除无效全局限流状态。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py` | 安全来源外链、有限数值、X 直链会话与缩略图代次/回收。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/video_list.py` | 协作取消、实际在途缩略图限流及 finished 回收。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/workers.py` | 线程保留/回收、有限缩略图网络与解码、逐跳 Cookie 范围。 |
| 新增 | `/Users/xxd/Playground/video-scrap/src/http_limits.py` | 流式字节预算、响应看门狗、取消与 HTTP 来源比较公共规则。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/js_decoder.py` | 相邻声明查找及受限静态解码输入预算。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | 版本绑定缓存、范围/直播/预算、有限预取、清单原子计划、错误恢复及停止资源归属。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/net.py` | 复用可取消会话工厂，准确说明 Session 并发约束。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/parallel_downloader.py` | 强验证器分段与单路长度进度、系统原子禁止覆盖发布。 |
| 新增 | `/Users/xxd/Playground/video-scrap/src/process_owner.py` | POSIX 私有进程组与 Windows kill-on-close Job Object。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/scraper.py` | 媒体直链/有限 HTML、逐层配置容错、数据数值规范化和协作取消。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/settings.py` | 串行提交、锁外有序通知、订阅去重/解除。 |
| 新增 | `/Users/xxd/Playground/video-scrap/src/temp_ownership.py` | 新目录所有者身份标记与保守后台回收。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/utils.py` | 有限数值显示、Windows 安全文件名及 URL 日志签名遮蔽。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/http_fixtures.py` | 使用真实 requests.Response 语义的有限 HTML 离线夹具。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_audio_downloads.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_audit_core_regressions.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_download_audit_oct18.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_download_cancellation.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_download_controls.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_download_name_reservation.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_gui_resource_boundaries.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_gui_thumbnail_network.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_hls_byte_ranges.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_hls_integrity_audio.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_hls_preview_master.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_media_proxy_cache_handles.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_media_proxy_ranges.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_parallel_downloader.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_process_ownership.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_proxy_audit_boundaries.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_config_hls.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_flow.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_network_boundaries.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_scraper_validation.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_tanstack_ev.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_test_runner.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_wrapped_hls_proxy.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_x_scraper.py` | 补充或调整对应模块回归/真实响应夹具，保留正常传输、取消和布局验证。 |

本轮共 38 个已跟踪文件修改、17 个新增文件，没有删除文件。

## 8. 最终验收摘要与下一步

- 回归：**46 个文件、508 项实际执行、0 跳过，全部通过**。采用完整隔离运行及最终改动对应的定向替换结果，不重复计数。记录：`/Users/xxd/Playground/video-scrap/build/audit-fixes-018/verified-results.json`。
- 实际源码应用：**15/15 项通过**，平台 Cocoa，`frozen=false`。记录：`/Users/xxd/Playground/video-scrap/build/audit-fixes-018/source-smoke-final-guarded.json`；日志：`/Users/xxd/Playground/video-scrap/build/audit-fixes-018/source-smoke-final-guarded.log`。
- HLS 双轨切换：**15/15 轮通过**。记录：`/Users/xxd/Playground/video-scrap/build/audit-fixes-018/stress-preview-results.json`。此压力检查在最后看门狗防护之前运行，最后防护由 32 项代理边界与最终应用检查复核。
- 全部 83 个 Python/spec 文件 AST 解析通过；源码哈希：`/Users/xxd/Playground/video-scrap/build/audit-fixes-018/verified-manifest.json`。空白差异检查通过。
- 环境：macOS arm64、CPython 3.12.14、PyQt6 6.11.0/Qt 6.11.2、yt-dlp 2026.8.19、requests 2.34.2、psutil 7.2.2；完整版本快照见新增 requirements 文件。
- 源码版本仍为 0.3.3，改动留在工作区。本轮没有提交、推送、打标签、发布或打包。
- 后续优先级：① 用户要求提交/推送时按规则递增该批次版本；② 重新打包并验证 macOS 成品；③ Windows 原生回归、打包、播放及 EXE 版本属性验收；④ 继续观察 3.1 的偶发 HLS 502，再考虑维护性拆分。③ 需要 Windows 或 GitHub Actions 的实际运行结果。
- 无标记历史目录不会被盲删；Windows、真实登录站点、长期直播和新安装包不属于本次本机通过的结论。

### 结果写入时间（Asia/Kuala_Lumpur，UTC+08:00）

| 节点 | 时间 | 结果 |
| --- | --- | --- |
| 完整隔离回归 | 2026-10-18 16:51 UTC+08:00 | 504 项通过 |
| 单路长度/进度回归 | 2026-10-18 16:52 UTC+08:00 | 39 项通过 |
| 清单与日志/GUI回归 | 2026-10-18 16:53 UTC+08:00 | 113 项通过 |
| 错误响应实体保护 | 2026-10-18 16:54 UTC+08:00 | 30 项通过 |
| 看门狗最终回归 | 2026-10-18 17:03 UTC+08:00 | 75 项通过 |
| 冻结源码实际应用 | 2026-10-18 17:03 UTC+08:00 | 15 项通过 |
| 无重复汇总 | 2026-10-18 17:04 UTC+08:00 | 508 项通过 |

记录生成：2026-10-18 17:04 UTC+08:00。
