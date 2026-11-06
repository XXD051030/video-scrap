# v0.3.4 当前代码只读复核

最终核验：2026-10-19 21:25 UTC+08:00，Asia/Kuala_Lumpur。

## 1. 结论与范围

本次以最新提交 `b8cf63298e592afcda8e88520e490610942937dc`、源码版本 `0.3.4` 为准，开始时工作树干净。重新阅读下载、进程归属、HTTP/代理缓存、播放器、抓取及相关设置/工具实现，结合既有测试与新输入追踪实际调用链。样式、图标、About 做结构复核；没有声称对第三方库和所有测试逐行完成形式化审计。

**确认 9 项问题：4 项 P2 优先处理、5 项 P3 次要问题。另有尚未验证的资源聚合和平台边界，不混入已复现 Bug 数量。**

优先级按实际影响和触发条件评估。特定 HLS 回退下确实发布了错误内容，应优先处理，但不能表述成普通下载无条件损坏；非法加密填充虽被接受，本次合成视频仍可播放，也不能据此宣称输出必然损坏。

本轮只增加本报告和忽略目录下的验证材料，没有修改业务代码、既有测试、版本、旧报告，没有提交、推送或打包。

| 编号 | 优先级 | 已确认的问题 | 实测结果 |
|---|---|---|---|
| R01 | P2 | HLS 后备下载重新读取变化后的清单，绕过 Range 保护 | 初始清单声明 2 秒，真实后备下载发布 4 秒文件并报成功。 |
| R02 | P2 | HLS 暂停时间被计入读取期限 | 真实暂停/恢复后失败；不暂停对照完成。 |
| R03 | P2 | JS 替换操作可绕过解码候选长度限制 | 369 字符输入产生 1,741,945 字符候选，超过 65,536 限制。 |
| R04 | P2 | 交互浏览器跨页混入旧视频，使用最后页面 Referer | 旧视频实际返回 403，换回正确来源返回 200。 |
| R05 | P3 | HLS 清单正文传输异常逃出代理处理器 | 截断/坏 gzip 直接断连接，缺少应用错误日志。 |
| R06 | P3 | 键盘操作进度条不实际 seek | 按 End 后滑条 60 秒，真实播放器仍在 20 秒。 |
| R07 | P3 | AES-CBC 无效 PKCS#7 填充未拒绝 | 非法零填充经解密、合并及发布后被当作成功。 |
| R08 | P3 | 一个巨大整数声明使整批 JS 隐藏源解码失败 | 普通视频保留，正常隐藏 HLS 源遗漏。 |
| R09 | P3 | yt-dlp 与 HTML 对同一视频未充分去重 | 单视频页面返回两个指向同一媒体的条目。 |

## 2. 优先处理的问题

### R01：HLS 后备下载的清单检查与实际使用脱节

**触发及现象**：首次读取的媒体清单是两个普通 1 秒片段、不含 BYTERANGE；原生分片请求收到 503 并耗尽重试。同一清单 URL 后来改为 BYTERANGE，服务器对范围请求错误地返回 HTTP 200 整对象。真实 yt-dlp 后备下载继续处理，FFmpeg 最终生成并发布 4 秒 MP4，而初次检查预计 2 秒。

**根因**：保存的是“已经检查过的 URL”，没有保存或固定实际检查过的清单内容。后备下载重新访问该 URL，实际内容不受原检查约束；输出校验只保护时长下界，重复整对象不会因更长而被拒绝。

**代码位置**：

- `/Users/xxd/Playground/video-scrap/src/downloader.py:600`：后备策略保存 URL。
- `/Users/xxd/Playground/video-scrap/src/downloader.py:618`、`:2069`：yt-dlp 再次读取清单。
- `/Users/xxd/Playground/video-scrap/src/downloader.py:1882`：时长校验只拒绝短缺。

**最终证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/downloads/valid-range-result.json`。FFmpeg 生成两个独立 1 秒 TS 片段，长度 40,984 与 37,412 字节，均按 188 字节对齐；合并为 78,396 字节对象，合法范围分别为 0–40,983 和 40,984–78,395。上游错误地给两次 Range 都返回完整对象，实际发布文件时长为 4.00 秒。初次失败由真实 HTTP 503 引起，没有模拟后端抛错；网络、yt-dlp、FFmpeg 及发布均实际执行。`decode-confirmation.json` 还确认产物可完整解码，证明“可播放”不等于“内容拼接正确”。

**范围限制**：需要“原生失败 + 清单内容变化 + 上游错误忽略 Range”的组合条件。实证为 yt-dlp 路径，不推断每种 FFmpeg 回退变体均已重放。旧 `boundaries-result.json` 的初次探针直接从 TS 中间切范围，保留为过程材料；最终结论采用上述合法独立 HLS 片段的复核证据。

**建议方向**：让后备策略也通过逐次验证的资源层，或使用固定清单/资源快照；仅增加一次预检查仍存在检查后变化窗口。时长双向检查可辅助，但不能代替实际字节范围与对象身份验证。

### R02：HLS 长时间暂停后恢复会误判超时

**触发及现象**：拉取 HLS 分片时暂停任务，暂停时间超过本次读取剩余期限；恢复后转为 failed，错误为 `_HlsIntegrityError: ... Response exceeded its total read deadline`。相同输入不暂停则正常完成。

**根因**：暂停通过操作系统挂起下载进程，但 `time.monotonic()` 绝对期限继续流逝；恢复时既未扣除暂停时间，也没有重建该次读取预算。任务允许暂停，却仍把这段主动暂停当成传输超时。

**代码位置**：

- `/Users/xxd/Playground/video-scrap/src/download_job.py:648`、`:659`：挂起/恢复进程。
- `/Users/xxd/Playground/video-scrap/src/downloader.py:1591`：分片总期限。
- `/Users/xxd/Playground/video-scrap/src/http_limits.py:103`、`:109`：正文期限检查。

**证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/downloads/paused-hls-result.json`、`probe_paused_hls.py`。真实 DownloadJob、spawn 子进程、进程组和暂停/恢复；仅将生产 120 秒期限缩为 0.3 秒，暂停约 0.50 秒。不暂停状态为 downloading → finalizing → completed；暂停对照恢复后 failed。

**范围限制**：没有实等 120 秒，不表示所有任务或在任意阶段暂停都会失败。该证据确认的是活动 HLS 读取预算没有排除暂停时间。

**建议方向**：预算只计算实际活动时间，或恢复时以新的、仍有完整性保护的请求重新开始；不应直接删除全部读取上限。

### R03：JS 字符串替换后的长度没有预算

**触发及现象**：受支持的 JS 自赋值链重复使用 `replace` 的前缀/后缀展开语义。369 字符页面、10 次替换后，实际返回 1,741,945 字符候选，远超声明的 65,536 字符上限。

**根因**：长度只在初始 fragments 拼接后检查；`_js_string_replace` 的 `$'` 等展开与连续表达式可以继续成倍增加字符串，没有逐次转换预算或输出总预算。

**代码位置**：

- `/Users/xxd/Playground/video-scrap/src/js_decoder.py:156`：替换展开。
- 同文件 `:170`：表达式链。
- 同文件 `:279`：仅检查初始 joined 长度。

**证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/root/decoder-expansion.json` 与 `decoder-expansion-repro.py`。0/5/8/10 次替换分别输出 29/7,168/193,549/1,741,945 字符。

**范围限制**：本轮有意停在小规模输出，没有制造内存耗尽或应用崩溃。实际证据是预算被绕过；继续展开可能导致高内存、CPU 占用，不能描述为已经观察到 OOM。

**建议方向**：在每次 URI 解码/替换前估计输出长度并拒绝超限，转换后再校验，并对单页总候选量设置上限。不能等巨大字符串已经分配后才只检查最终结果。

### R04：浏览器跨页素材与页面来源混用

**触发及现象**：同一交互浏览器先打开 `/old`，再导航到 `/new`，最后 Use videos 同时返回两页的视频和最后页标题。主窗口给两条视频都设置 `/new` Referer；本机旧视频要求 `/old`，实际返回 403，正确来源对照为 200。

**根因**：网络/DOM 素材集合只累加，页面改变时只更新 `page_url`；异步 DOM 提取也没有页面代次。结果缺少每条素材的来源关系，主窗口再统一覆盖为最后页面来源。

**代码位置**：

- `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:295`、`:304`、`:326`、`:350`。
- `/Users/xxd/Playground/video-scrap/src/gui/main_window.py:940`、`:945`。

**证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/gui/reproduction.json` 的 `navigate`。真实本机 Chromium 页面导航、MainWindow 导入、项目请求头构建及 403/200 HTTP 对照，没有真实站点或登录 Cookie。

**建议方向**：按导航代次隔离捕获和 DOM 回调；如果产品要累计多个页面的视频，则逐条保留来源及 Referer，不能把所有旧素材视为最后一页内容。

## 3. 影响较小的确认问题

### R05：清单正文的 Requests 异常未转成可诊断响应

- **现象**：上游 HLS 已返回 200，但正文截断或声明 gzip 却发送坏内容时，播放器端收到 `RemoteDisconnected`；应用状态日志为空，HTTP 线程打印未捕获堆栈。
- **根因**：正文读取产生 `requests.ChunkedEncodingError` / `ContentDecodingError`，现有 except 只列内建异常，没有覆盖 Requests 传输异常。
- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1656`、`:1687`。
- **证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/proxy/manifest-transport-result.json`。42 字节本机清单，正常 200，截断与错误 gzip 两种负例均直接断连。
- **影响边界**：错误反馈与网络故障处理缺口，没有证据表明整个应用崩溃或错误字节进入缓存。将其列为 P3；优先级低于错误成品和正常暂停失败。
- **建议**：发送响应头前捕获预期网络异常，记录明确错误，并通过已有有限长度 502 输出处理，保留取消与清理语义。

### R06：进度条键盘动作只改变显示值

- **现象**：真实 60 秒本地 WAV 暂停在 20 秒，聚焦进度条按 End；slider=60000，player.position=20000。
- **根因**：只监听鼠标拖动的 `sliderMoved`，没有处理键盘 action 引起的位置变化。
- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:227`。
- **证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/gui/reproduction.json` 的 `keyboard_end`；真实 QTest 与 QMediaPlayer，离屏 Qt。
- **影响边界**：已确认 End；不推断每个平台所有辅助输入均已测试。鼠标点击/拖动的旧问题不是本项，音量监听 valueChanged，不存在这个相同缺口。
- **建议**：识别用户键盘/滚轮动作并 seek，同时避免播放器位置回传引起递归 seek。

### R07：加密片段无效 PKCS#7 填充被原样接受

- **现象**：使用非法零填充的合成 AES-CBC TS，经真实解密、FFmpeg 合并和最终发布后报成功。
- **根因**：末块只检查 AES 块对齐；`_strip_pkcs7_padding` 对不合法填充直接返回原文，没有拒绝。
- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:1667`、`:2361`。
- **证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/downloads/boundaries-result.json` 的 `invalid_aes_padding`。HTTP 输入为受控 fake，AES、FFmpeg 及发布真实执行。
- **影响边界**：本次只是尾部填充不合法，生成视频仍可播放；不是已证实的画面损坏。填充检查也不能取代认证或校验全部篡改。
- **建议**：对必须使用 PKCS#7 的 AES-128 HLS 校验末块，不合法应明确失败。若要兼容不规范站点，需要显式且可追踪的策略。

### R08：巨大整数声明阻断同页其他隐藏源解码

- **现象**：同页正常视频与隐藏 HLS 对照均可发现；加入一个无关 5,000 位整数声明后，普通视频仍有，隐藏 HLS 丢失。
- **根因**：`json.loads` 超过当前 Python 整数字符串限制会抛 ValueError；整数数组解析只捕 JSONDecodeError，异常越过整个候选声明扫描。scraper 捕获整次 decoder 异常后改用空隐藏源列表。
- **位置**：`/Users/xxd/Playground/video-scrap/src/js_decoder.py:71`、`:98`；`/Users/xxd/Playground/video-scrap/src/scraper.py:612`。
- **证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/root/decoder-integer.json`；真实 scrape 调用链对照在 `gui/scraper-decoder-boundary.json`：145 字符页面有 normal.mp4 + hidden.m3u8，5,159 字符负例只剩 normal.mp4。
- **影响边界**：不是整页抓取崩溃；依赖当前 Python 的整数长度保护，特殊输入触发。
- **建议**：在单个声明层面限制/拒绝异常整数并继续扫描其他候选，不应靠调高全局 Python 整数限制处理网页内容。

### R09：同一媒体跨抓取策略重复显示

- **现象**：本机只有一个 video 元素，真实 yt-dlp 和 HTML 扫描返回两个条目：一个网页条目及其唯一 MP4 格式，一个同地址 direct MP4。
- **根因**：跨策略只按条目的顶层 URL 去重，没有识别提取器条目中的同一确定媒体格式 URL。
- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:539`、`:561`。
- **证据**：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/gui/ytdlp-html-overlap.json`、`probe_ytdlp_html.py`。真实本机 yt-dlp，无网络 mock。
- **影响边界**：列表冗余；未证明文件损坏。仅验证单媒体 generic extractor，不能直接合并所有清晰度或不同播放列表条目。
- **建议**：在保留提取器元数据的同时，仅对可以确定相同的媒体跨策略去重。

## 4. 旧问题与未验证边界

### 旧问题没有原样重新列入

- N01–N08 的已有触发测试仍通过；本轮 R01/R02/R03 等是新的检查/使用、暂停和转换后边界，不是把旧报告的描述直接复制为未修复。
- C01 成功切换角色后的旧缓存复用，当前使用新 Entry/token/cache 隔离；相关 4 项测试通过，本轮没有复现旧路径。
- POSIX 历史 PGID 信号风险已有结构性调整：监督器通过管道控制持续存活的组长，只有组长向自身当前组发信号。7 项本机所有权测试通过；未发现原先监督器向历史 PGID 发 killpg 的路径。不能再照搬“仍未修复”的旧状态。
- 当前鼠标拖动释放、超长时长映射、音量/静音、全屏、列表折叠、日志展开及正常下载控制均通过既有检查。

### 条件观察，不计入 9 项 Bug

- 代理资源数上限按单清单统计，嵌套清单的整体条目/临时文件元数据占用缺少小规模全局上限；未做耗尽实验，没有依据宣称已 OOM 或已绕过字节缓存配额。
- Windows 原生 Job Object、Windows 文件系统及安装包没有本轮实机证据。
- 本轮未重新打包、未运行 GitHub 远程 CI，未验证真实 TLS/外部代理和真实站点的长时间播放。
- 历史单次 HLS 502 的根因仍不能由这次本地结果倒推。R05 是另一个已复现的传输异常处理缺口，没有证据指认它就是历史那一次的原因。
- 任意无标识路径秘密的日志识别、任意文件系统总路径长度等既有能力边界保留，不升级为新的已复现故障。

## 5. 本轮验证材料

### 既有完整回归

```bash
cd /Users/xxd/Playground/video-scrap
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python scripts/run_tests.py \
  --output build/audit-current-2026-10-19/tests --timeout 150
```

结果：**52 个模块，568 项执行，0 失败、0 错误、0 跳过**。报告为 `/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/tests/summary.json`。分组复核重复执行的 109/87 项不重复累计。

### 实际源码应用集成

```bash
cd /Users/xxd/Playground/video-scrap
PYTHONDONTWRITEBYTECODE=1 PATH='' \
  /Users/xxd/Playground/video-scrap/build/.venv/bin/python \
  /Users/xxd/Playground/video-scrap/main.py --smoke-test \
  /Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/source-smoke.json
```

**15 项通过**，实际 macOS Cocoa、源码 0.3.4、frozen=false、空 PATH。包含真实视频帧、直链/HLS/包装 HLS、音频导出、WebEngine、主题、Preferences、About、全屏及逐任务暂停/恢复/取消；只访问本机夹具，没有使用浏览器凭据。

注意：这些既有通过结果没有覆盖本报告新增的失败条件。复现脚本成功退出表示成功证明缺口，不等于该功能通过。

### 快照与环境

- 源码/测试/配置快照 95 文件：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/source-manifest.json`。
- 最终核验汇总：`/Users/xxd/Playground/video-scrap/build/audit-current-2026-10-19/verification.json`。
- 业务源码、既有测试和配置首尾哈希不变；Python/spec AST 解析通过。
- 运行解释器：`/Users/xxd/Playground/video-scrap/build/.venv/bin/python`，Python 3.12.14，当前 macOS arm64。
- 源码实际集成输出及日志：`source-smoke.json`、`source-smoke.log`，均在本轮 build 目录。
- 下载探针只挂起、恢复和清理自己创建的进程；未信号无关进程、未制造 OOM 或磁盘写满。Cocoa/回环端口检查按工具权限运行；初次受限端口报错未当成产品缺陷。

## 6. 建议处理顺序

1. R01：先修后备下载的实际资源验证，避免错误成品被发布。
2. R02、R03：处理用户暂停语义与网页解码预算。
3. R04：隔离交互浏览器导航结果及逐条来源。
4. R05–R09：完善网络错误反馈、键盘 seek、加密输入校验、声明容错和去重。
5. 修复后给上述新边界增加针对性回归，再验证完整套件、macOS 安装包及 Windows 原生行为。

本轮结论是“当前证据支持的剩余问题”，没有给出“所有代码已经没有其他隐患”的保证。
