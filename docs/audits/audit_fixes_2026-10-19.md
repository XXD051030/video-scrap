# 第二轮复核问题修复记录

记录时间：2026-10-19 01:09 UTC+08:00（Asia/Kuala_Lumpur）。

## 1. 范围与结论

本轮处理 `/Users/xxd/Playground/video-scrap/code_audit_recheck_2026-10-18.md` 中的 N01–N08，并补上复核过程中确认的响应正文取消与连接池复用竞态。以上触发路径均已修改，并有针对性回归支持；这不等于整个代码库已不存在其他缺陷。

- Git 基点：`63194eafd892734259f2a069d76b042693ea090c`，另有上一轮尚未提交的工作区修改。
- 源码版本：`0.3.3`，本轮没有改版本、提交、推送或重新打包。
- 本轮以保存的审查源码哈希为比较基准，修改 10 个既有文件、新增 5 个测试文件。此前已经存在的修改、资产、依赖快照及历史报告保留。
- 完整回归：**51 个测试模块，559 项执行，0 失败，0 错误，0 跳过**。
- 源码应用集成：**macOS Cocoa 真实窗口，15 项通过**，运行时实际设置 `PATH=''`，没有模拟 FFmpeg 查找结果。
- 另列的 POSIX PGID 复用条件风险仍保留；Windows 原生、新安装包和真实远程长时间播放未在本轮验收。

## 2. 时间与执行顺序

早期逐项开发的精确起止时间没有完整记录，标记为 `[待确认]`；以下最终验收时间来自文件时间及当前时钟，均使用 UTC+08:00。

| 时间 | 目的与操作 | 结果 |
|---|---|---|
| `[待确认]`，最终验收前 | 对照第二轮审查报告与工作区，分别检查 GUI、下载、代理、HTTP 生命周期及测试入口；为已确认问题补边界测试。 | 保留 N01–N08 的独立编号；不把旧修复声明直接作为证据。 |
| `[待确认]`，最终验收前 | 分组实施修复并交叉复核；使用真实 30 天媒体、受控 HTTP、socketpair、故障注入检查边界。 | 另外确认正文取消与连接复用之间仍需归属互斥，随后补上代次保护。 |
| 2026-10-19 01:05 UTC+08:00 | 冻结源码、保存 SHA-256 清单，运行完整隔离回归与清空 PATH 的真实窗口集成检查。 | 559 项回归及 15 项集成检查通过。 |
| 2026-10-19 01:06 UTC+08:00 | 对照验收前后的源码哈希，完成连接归属保护的独立只读复核，并整理本记录。 | 受检源码哈希未变化；未发现本轮连接保护的可证明阻塞项。 |

## 3. 逐项 Bug 修复

### N01：合法超长媒体使进度条溢出并可能崩溃

- **现象**：超过约 24.86 天的合法媒体时长传给 QSlider 后出现 OverflowError；原复现进程 SIGABRT。
- **根因**：QMediaPlayer 的毫秒值为 64 位，QSlider 只接收 32 位整数；时长、位置、seek 未做统一转换。
- **修复**：普通媒体保留精确毫秒刻度；超长媒体以整数运算映射到 `2^31-1`，保留原始 64 位毫秒值。更新播放位置、拖动、时长变化、全屏与 seek 均通过同一映射。
- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:871`，映射方法在同文件 `:896`。
- **验证**：新增 4 项回归。真实播放器加载 1,452 字节、30 天时长 MP4，读到 `2592000000` 毫秒，成功定位到 `2591998999` 毫秒，槽函数无异常、进程正常退出。
- **证据**：`/Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/gui-smoke/long-duration.log`。

### N02：包装 HLS 在共享缓存满后持续 502

- **现象**：点播包装分片累计占满共享缓存后，后续分片及重试无法播放。
- **根因**：把“写入缓存失败”当作“完整有效媒体不可播放”，播放路径必须先获得缓存配额。
- **修复**：缓存拒绝时，直接向播放器提供请求私有的、有大小限制的解包正文，支持 HEAD、普通/尾部 Range、重试和回看；不额外创建磁盘暂存。全局最多 2 个包装正文处理槽，覆盖获取、解包、输出及慢速错误回复。预取失败在槽内消化，避免已完成 Future 通过异常回溯保留正文。
- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1344`、`:1438`、`:1487`、`:1515`、`:1524`。
- **边界**：共享缓存原有 512 MiB 配额仍在；单个解包媒体上限仍为 64 MiB。两个槽限制的是并发正文生命周期，不表示 Python 总内存精确等于 128 MiB，编码包络及解码中间对象另占内存。
- **验证**：小缓存预算下验证后续分片、HEAD、Range、尾部定位、重试；慢速输出与慢速错误回复下验证槽数量；验证完成的失败预取不再持有解包正文。未实际写满磁盘。

### N03：整文件播放被 120 秒绝对期限截断

- **现象**：整部 MP4 从当前位置读到文件尾时，即使持续有数据，也可能超过分片用的绝对读取期限。
- **根因**：整文件响应与有限 HLS 片段使用同一计时方式；播放器慢速消费时间也计入总期限。
- **修复**：仅整文件根资源改用进展/空闲超时；采用 `read1` 识别少量到达的数据，向播放器输出期间暂停上游空闲计时，取消仍生效。保留正文大小上限、请求/套接字超时；有限 HLS 子资源、清单、密钥仍保留绝对期限。
- **位置**：`/Users/xxd/Playground/video-scrap/src/http_limits.py:169`；代理选择位于 `/Users/xxd/Playground/video-scrap/src/media_proxy.py:1983`、`:2021`、`:2139`。
- **验证**：缩短时间预算重放各整文件分支；持续慢速传输可以跨越旧总期限；慢速播放器不被误判；真实本地 HTTP 滴流、实际阻塞、取消及字节上限均有回归。未把缩短预算测试描述为连续播放了 120 秒以上。

### N04：重定向到不同资源、相同 ETag 时拼错字节

- **现象**：原 URL 在两个最终资源间变化，二者长度及强 ETag 字符串相同，下载或缓存仍可能混合两个对象的数据。
- **根因**：强 ETag 只在对应资源内有意义，原实现没有把最终响应 URI 纳入身份。
- **修复**：统一规范最终 HTTP(S) URI，并与强 ETag、长度共同校验。协议/主机/默认端口规范化，去掉 fragment；路径与查询原样保留，不重排签名参数。直接分段下载、HLS 字节范围、无验证器整对象回退及代理缓存均检查最终身份；身份变化时拒绝组合。不同原 URL 指向同一最终对象的合法情况仍可工作。
- **位置**：`/Users/xxd/Playground/video-scrap/src/http_limits.py:12`；`/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:265`、`:452`；`/Users/xxd/Playground/video-scrap/src/downloader.py:1559`；`/Users/xxd/Playground/video-scrap/src/media_proxy.py:516`。
- **验证**：受控响应与真实本地重定向服务验证不同最终对象被拒绝、稳定目标正常、同目标别名正常、无验证器回退不能改指其他对象。原 D04 应表述为“原路径及本次 N04 变体有保护”，不能直接推广为任意服务端内容一致性证明。
- **取舍**：签名参数变化也视为 URI 变化，采用安全失败；未实现签名更新后自动重新探测整部影片。

### N05：头部 Timer 返回成功后仍关闭连接

- **现象**：Timer 回调已经越过内部取消检查时，`cancel()` 仍可能无法阻止迟到 shutdown。
- **根因**：只有取消 Timer，没有把“响应头已完成”和“回调有权关闭套接字”作为互斥状态。
- **修复**：头部完成、超时状态及回调 shutdown 共用生命周期锁；成功返回前撤销 Timer 的关闭权限；超时先获得权限则拒绝该响应。
- **位置**：`/Users/xxd/Playground/video-scrap/src/browser_cookies.py:172`。
- **验证**：使用确定性交错和真实 socketpair，分别覆盖成功返回后的迟到回调、超时先发生的拒绝路径。

### N06：原生 HLS 清单产生大量模型与排队 Future

- **现象**：一个 EXTINF 后跟大量 URI 可被解析成大量分片；合法长清单也一次性提交全部 Future。
- **根因**：清单字节上限没有约束资源数；解析器复用时长，线程数只限制运行数量，未限制等待队列。
- **修复**：每个媒体 URI 必须有独立、有限、正数 EXTINF，先验证整份结构与资源数再读取密钥；每份媒体清单最多 100,000 个资源（包含计入的密钥/初始化 URI）。采用滑动提交窗口，未完成 Future 最多 `2 * workers`，支持视频与外部音轨交错补充；畸形清单不能绕过检查走其他下载后端。
- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:55`、`:1050`、`:1337`。
- **验证**：缺失/重复/非法时长、资源越界、密钥读取前拒绝、后端回退、长清单取消、两轨完成顺序与队列峰值均有回归。一天的一秒分片清单仍被接受；音视频各有一份清单时分别受限，并非两轨总共只允许 100,000 个资源。
- **既有测试调整**：为一个原正向 HLS 范围夹具补上必需的 EXTINF，保持“合法输入应成功”的测试目的。

### N07：失败清单刷新污染既有子资源角色

- **现象**：失败刷新把已有片段改成清单类型；原清单重试后仍错误解析 TS。
- **根因**：事务回滚只删除新增子条目，没有恢复旧条目的类型、索引及父级播放计划。
- **修复**：事务快照包含既有子资源的 `force_manifest`、扩展、索引、退休时间，以及 URI 查找表、分片/时长/范围列表、序列、直播状态、播放锚点与预取代次。失败恢复整个快照；成功刷新允许片段与清单双向切换。
- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:2193`、`:2229`、`:2388`。
- **验证**：解析失败、提交阶段故障注入均恢复旧计划；成功的双向角色切换正常。

### N08：源码自检错误地要求 PATH 中存在 FFmpeg

- **现象**：应用能通过 build assets/imageio 找到 FFmpeg，但源码集成检查提前报缺少依赖。
- **根因**：自检与应用使用了不同的查找规则。
- **修复**：源码自检复用 `src.ffmpeg.resolve_ffmpeg()`；frozen 模式另行严格校验解析路径必须是 `_MEIPASS/bin/ffmpeg`（Windows 为 `ffmpeg.exe`），避免系统工具掩盖包内缺失。
- **位置**：`/Users/xxd/Playground/video-scrap/scripts/smoke_app.py:21`。
- **验证**：FFmpeg 模块新增 9 项测试；源码真实运行在 `PATH=''` 下仍通过全部 15 项集成检查。frozen 来源门禁已做回归，本轮未生成新安装包。

### 补充：正文取消误伤连接池中的下一次请求

- **现象**：正文看门狗抓取到旧套接字后，读线程可能完成并归还连接；迟到取消可影响后来的请求。
- **根因**：完成检查与 shutdown 之间，连接归属可能变化；只检查 `raw.closed` 不能构成完整的原子保证。
- **修复**：自有连接增加代次与互斥；HTTP/HTTPS 的 `request` 和 `connect` 在网络 I/O 前换代。响应绑定捕获的套接字与代次；检查、shutdown、连接及原响应关闭在同一锁内完成。旧代次失效后不再尝试关闭旧包装对象。HTTP/1.0 已从连接分离但仍持有正文的套接字保留可取消能力。
- **位置**：`/Users/xxd/Playground/video-scrap/src/browser_cookies.py:151`、`:159`、`:204`、`:230`；`/Users/xxd/Playground/video-scrap/src/http_limits.py:36`。
- **验证**：新增 6 项，包括确定性 EOF/释放交错、request/connect 换代顺序、HTTPS connect 顺序、拒绝后不走关闭回退、真实同套接字 keepalive 复用及 HTTP/1.0 阻塞正文取消。独立只读复核未发现反向锁顺序。
- **限制**：没有归属钩子的其他适配器仍是尽力中断，必须使用有限读取超时；未实测真实 TLS 或外部代理。没有证据把此前偶发 HLS 502 的历史根因直接归到此竞态。

## 4. 本轮文件变更清单

下表相对于第二轮审查的保存源码，不等于相对于 HEAD 的全部历史未提交改动；完整受检源码哈希见第 5 节。

| 类型 | 完整路径 | 摘要 |
|---|---|---|
| 修改 | `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py` | 64 位媒体位置与 32 位进度条统一映射。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/browser_cookies.py` | 头部 Timer 生命周期互斥与连接代次归属保护。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/http_limits.py` | 最终 URI 身份、整文件进展超时、受归属保护的正文中断。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | 缓存满时的受限包装正文、根资源读取策略、最终身份及完整清单回滚。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/downloader.py` | HLS 身份、结构/资源校验、有界 Future 窗口。 |
| 修改 | `/Users/xxd/Playground/video-scrap/src/parallel_downloader.py` | 分段范围与探测的最终 URI 一致性。 |
| 修改 | `/Users/xxd/Playground/video-scrap/scripts/smoke_app.py` | 源码共用 FFmpeg 解析器，安装包保持严格来源门禁。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_ffmpeg_resolver.py` | 新增 9 项自检查找规则与 bundle 来源回归。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_hls_byte_ranges.py` | 正向清单夹具补齐 EXTINF。 |
| 修改 | `/Users/xxd/Playground/video-scrap/tests/test_wrapped_hls_proxy.py` | 并发到达门改在请求入口统计，保留缓存复用只抓取一次的断言。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_preview_long_duration.py` | 4 项超长位置、拖动、时长变化与全屏回归。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_download_recheck_oct18.py` | 11 项最终对象身份、严格清单、有界排队与正常两轨回归。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_proxy_recheck_oct18.py` | 12 项缓存满、正文生命周期、整文件进展、URI 身份及回滚回归。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_http_lifecycle_recheck.py` | 9 项 Timer 交错、进展/空闲、取消、字节预算及 URI 规范化回归。 |
| 新增 | `/Users/xxd/Playground/video-scrap/tests/test_connection_lease_recheck.py` | 6 项归属锁、代次、真实复用及 detached 正文回归。 |
| 新增 | `/Users/xxd/Playground/video-scrap/audit_fixes_2026-10-19.md` | 本次修复、证据及剩余边界记录。 |

本轮没有保留删除文件。`src/process_owner.py`、`src/download_job.py`、`tests/test_process_ownership.py` 最终哈希与审查快照相同，不能据临时试验结果宣称修复了 PGID 风险。

## 5. 最终验收与复现入口

### 完整隔离回归

```bash
cd /Users/xxd/Playground/video-scrap
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python scripts/run_tests.py \
  --output build/audit-fixes-2026-10-19/final-regression --timeout 150
```

结果：51 个模块，559 项执行，0 失败、0 错误、0 跳过；相对于旧 508 项增加 51 项（5 个新模块 42 项，加既有 FFmpeg 模块 9 项）。分组开发阶段的重复执行不再叠加计数。

- 汇总：`/Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/final-regression/summary.json`。
- 各模块详细日志、断言及结果位于同一目录。

### 真实源码应用集成

```bash
cd /Users/xxd/Playground/video-scrap
PYTHONDONTWRITEBYTECODE=1 PATH='' \
  /Users/xxd/Playground/video-scrap/build/.venv/bin/python \
  /Users/xxd/Playground/video-scrap/main.py --smoke-test \
  /Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/source-smoke-final.json
```

15 项全部通过：依赖、主窗口、黑白主题、Preferences、About、空预览全屏、媒体夹具、直接下载、字节范围 HLS 下载、音频导出、实际 Qt 直链播放、HLS 主清单音视频预览、包装 HLS 预览、WebEngine、逐任务暂停/恢复/取消。只连接 `127.0.0.1`，没有使用浏览器凭据。

- 实际解出的直接下载正文与样本逐字节一致。
- 原格式/MP3/M4A 在直接、HLS、外部音轨路径均成功解码且仅有音频。
- 预览采集到真实视频帧；包装 HLS 的 `player_errors` 为空。
- 暂停后进度冻结、其他任务继续；恢复字节一致；取消后无输出、暂存清理。
- 报告：`/Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/source-smoke-final.json`。
- 日志：`/Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/source-smoke-final.log`。

### 静态与版本绑定

- 88 个项目 Python/spec 文件通过 AST 解析；`git diff --check` 通过。
- 验收前后源码 SHA-256 比较无变化：`/Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/source-manifest.json`。
- 因环境 Cocoa 在受限上下文可能报告没有屏幕，真实窗口验收使用已获工具批准的本机执行环境；未用离屏成功替代真实 Cocoa 成功。
- 中间开发阶段的失败或已撤回试验输出不作为最终通过证据；最终以本节指定汇总为准。

## 6. 环境

本轮本机实际检测：

```text
macOS 27.0.1 (26A434), arm64
Python 3.12.14
Interpreter: /Users/xxd/Playground/video-scrap/build/.venv/bin/python
PyQt6 6.11.0; PyQt6-Qt6 6.11.2
requests 2.34.2; urllib3 2.8.0
yt-dlp 2026.8.19
PyInstaller 6.22.3; imageio-ffmpeg 0.6.0
FFmpeg resolved: /Users/xxd/Playground/video-scrap/build/app-assets/bin/ffmpeg
Qt platform: cocoa (source integration), offscreen (isolated GUI regressions)
Source app version: 0.3.3; frozen=false
Playback proxy: 127.0.0.1, ephemeral port (this smoke run: 61705)
```

依赖快照：`/Users/xxd/Playground/video-scrap/build/audit-fixes-2026-10-19/gui-smoke/requirements-freeze.txt`。本轮未新增数据库，也未修改用户网络或 DNS 配置。媒体夹具、报告和测试临时资料位于 build/测试临时目录；没有清理用户下载文件。

## 7. 保留问题与下一步

| 优先级 | 状态 | 边界与下一步 |
|---|---|---|
| 后续专项设计 | **[未解决] POSIX 裸 PGID 复用条件风险** | 尚未观察到误杀，但根进程被回收后裸号码不再足以证明归属。需要重新设计组身份与回收顺序，同时考虑 multiprocessing 会自动回收其他已结束子进程；不能只在某个 join 调用处延迟回收。本轮三个相关文件已恢复审查时内容。 |
| 发布前 | **[未验证] Windows 原生及 GitHub CI** | 本机 macOS 通过不代表 Windows Job Object、打包及远程 CI 通过。需要在 Windows 执行同一套测试与应用集成。 |
| 发布前 | **[未开始] 新安装包** | 本轮只改源代码。已有 App/ZIP 不会自动获得这些修复，需另行打包和 frozen 验收。 |
| 后续观察 | **[待确认] 历史偶发 HLS 502 根因** | 当前最终集成未出现播放器错误；已修复两个确定性竞态，仍不能证明历史单次 502 就由它们造成。复现后应保留请求、错误类别及生命周期日志再归因。 |
| 后续兼容 | **[未验证] 真实 TLS、外部代理、长时间远程播放** | HTTPS 所有权顺序有确定性测试，尚未做真实 TLS 流量；短预算和本地媒体验证不能替代真实站点长期运行。 |
| 用户授权后 | **[未开始] 提交/推送与版本更新** | 保留当前工作区；下次获准推送项目改动时，按项目规则为该批修改更新一次版本，并同步源码、macOS spec 与 README。 |

本轮未修改历史审查和日记，以保留当时的事实。后续复核应使用本轮源码哈希与新增触发测试，而不是只依据“测试通过”或历史报告中的概括。
