# 当前代码全面审查记录

审查日期：2026-10-18，Asia/Kuala_Lumpur（UTC+08:00）。项目：/Users/xxd/Playground/video-scrap。

**结论：现有正常流程的回归检查全部通过，但仍确认了 23 项缺陷或输入、资源边界缺口。优先处理下载完整性、凭据传递、外链属性注入，以及预取和线程退出问题。另有 12 项条件性风险/设计约束和 7 项工程改进，不能与已经复现的用户故障混为一谈。**

基准提交为 9f8dd8c5c1b0985a6804136db81c36e5db859e8e，源码版本 0.3.2。审查对象是当前磁盘上的工作区，包含已有未提交修改和新增代码，不只是该提交或 git diff。行号对应本次审查时的文件。

本次没有修改业务代码、测试、版本、历史报告，也没有提交、推送或重打包。新增本报告和 build/audit-2026-10-18/ 下的审查证据。没有读取真实浏览器凭据或访问真实登录站点。

“全面阅读”指项目维护的 Python、打包配置和测试文件均纳入检查，不包括逐行审计第三方库、生成产物、旧安装包或历史日记。一次审查不能证明找到了所有可能的问题，也不能用通过的测试代替未覆盖场景的证明。

**一、范围、结构及验证结果**

完整阅读的实现与工具包括 30 个 Python 文件，共 13,335 行；另外阅读 2 个 PyInstaller spec 文件。检查 38 个测试文件并逐文件执行。还核对了依赖清单、README、Windows 构建说明、忽略规则和 SVG 资源。

| 层次 | 入口和职责 | 主要跨层关系 |
|---|---|---|
| 启动/实例 | main.py、single_instance.py | Qt 初始化、单实例锁和激活、退出后监督线程收尾 |
| 界面 | main_window.py、preview_panel.py、video_list.py | 抓取代次、选择预览、下载排队、任务行、窗口和线程生命周期 |
| 页面提取 | scraper.py、js_decoder.py、interactive_scraper.py、js_renderer.py | yt-dlp、HTML、播放器配置、受限静态 JS 解码、Chromium 回退 |
| 会话 | browser_cookies.py | Qt Cookie 快照、严格作用域 CookieJar、每次请求重新应用 Cookie 策略 |
| 下载 | download_job.py、downloader.py、parallel_downloader.py | 后台监督线程管理独立进程，GUI 发控制；后端下载、HLS 合并/音频转换、私有暂存、成品发布 |
| 预览代理 | media_proxy.py | 回环 HTTP、Range、清单重写、缓存、顺序及 HLS 并行预取 |
| 通用支持 | settings.py、net.py、ffmpeg.py、hls_payload.py、utils.py | 配置、连接池、FFmpeg 发现、PNG 包装校验、显示/文件名工具 |
| 打包与验证 | scripts/、app_bundle/ | 目标系统构建、资源/许可证收集、源码/安装包检查记录 |

本次验证结果：

- **38 个回归文件全部通过，共 411 项检查**：日志报告的 unittest 用例 303 项，其余函数式入口 108 项；独立进程运行避免 Qt 全局状态相互干扰。
- **源码应用集成检查 15 项全部通过**：包含实际 Cocoa/Qt 播放、全屏、HLS、PNG 包装、音频格式、WebEngine 和下载暂停/恢复/取消。合计 426 项既有回归/集成检查通过。
- 68 个项目 Python 文件均完成 AST 解析；git diff --check 通过。
- 新增定向复现另计，不能将“成功证明缺陷”的脚本退出 0 解读为产品不存在缺陷。
- 最初普通 .venv 缺少 psutil；后来统一使用依赖齐全的 build/.venv。部分测试最初被沙箱禁止绑定回环端口、IPC 或 Chromium MachPort，放开对应限制后重跑通过；这些环境拒绝没有计为产品故障。
- 实际验证环境：macOS arm64、Python 3.12.14、PyQt6 6.11.0、Qt 6.11.2、yt-dlp 2026.8.19、requests 2.34.2、psutil 7.2.2。没有验证 Windows 本机、新安装包、最低依赖版本组合或长期真实直播。

证据：[测试汇总](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/final-test-results.json)、[源码集成结果](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/source-smoke.json)、[源码哈希](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/source-manifest.json)。

**二、已确认的缺陷与边界缺口**

P2 表示应安排修复的实质问题；P3 表示较低影响、特定交错或较少使用的接口问题。这些等级是本项目中的修复优先级，不是漏洞评分。标注“静态确认”意味着代码缺少该保护，不能据此宣称本次发生了内存耗尽、凭据外泄或系统崩溃。

**D01 · P2 · 普通 HLS 最终 FFmpeg 回退会把缺片结果当完整下载【真实媒体复现】**

位置：[downloader.py:1953](/Users/xxd/Playground/video-scrap/src/downloader.py:1953)，关联 [downloader.py:611](/Users/xxd/Playground/video-scrap/src/downloader.py:611)。

当前最终回退主要依赖 FFmpeg 退出码；普通单轨路径没有执行相同的完成时长验证。FFmpeg 遇到缺片时可能跳过并返回 0，因此“成功退出”不等于“完整下载”。

离线合成 3 秒 HLS，删除尾片，再让前两种后端失败以进入该回退。完整 download() 发布 missing-tail.mp4，实际只有 **2.03 秒**，最终仍为 completed。这里模拟的是回退触发条件，使用的合并和输出媒体是真实 FFmpeg。

建议所有 HLS 回退继承选定清单的期望时长和完整性要求，拒绝跳片后成功；增加缺首片、中片、尾片的回退测试。

**D02 · P2 · 双轨验证无法识别音轨提前结束【真实媒体复现】**

位置：[downloader.py:1617](/Users/xxd/Playground/video-scrap/src/downloader.py:1617)。

验证只检查容器总时长，以及是否存在音视频流。长视频轨会掩盖已经提前结束的音轨。仅修 D01、统一调用现有验证函数，仍不能解决此问题。

本地 3 秒视频/约 3.029 秒独立音频的主清单，删除后段音频片段，并模拟前两种后端失败以进入回退。require_av=True、期望总时长约 3.03 秒的验证仍通过；成品视频 **3.000000 秒**、音频 **2.005333 秒**，任务显示完成。

建议分别验证每条所选轨道的时间覆盖，处理开头偏移、允许的尾差和缺片，而不是只看容器最大时长。

**D03 · P2 · 直链把空文件或登录错误页当视频发布【完整流程复现】**

位置：[parallel_downloader.py:262](/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:262)，关联 [downloader.py:421](/Users/xxd/Playground/video-scrap/src/downloader.py:421)。

HTTP 200 和长度吻合即可通过；最终结果只要求是普通文件。会话失效或反爬返回 200 HTML 时，会保存成 mp4 并提示完成。离线响应分别发布 **0 字节 empty.mp4** 和 **52 字节、Content-Type 为 text/html 的 login.mp4**。

建议至少拒绝空媒体和明确的 HTML/JSON 错误响应，对可疑正文检查媒体容器；不能仅靠扩展名认定视频有效。任意合成字节的传输测试应继续保留，另加媒体验收层测试。

**D04 · P2 · 并行下载和代理缓存会拼接同 URL 的不同资源版本【离线响应复现】**

位置：[parallel_downloader.py:245](/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:245)、[media_proxy.py:1560](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1560)。

当前严格检查 Range 位置和总长，但不将后续请求绑定到同一强 ETag 等资源标识。同 URL 内容在传输中变化、长度不变时，所有区间仍可能合法，最终文件却由两个版本组成。

下载复现：8,192 字节输出含 2,048 个 A 和 6,144 个 B，不等于任一原版本；代理复现：两个不同 ETag 的 206 被缓存成 AABB。请求没有相应实体条件。稳定的不可变签名 URL 会降低概率，但代码没有保证资源不可变。

建议探测时绑定资源验证器，对每段使用适当条件并核对响应，变化时清空缓存/重启传输。单纯补长度验证无效。

**D05 · P2 · 非 Cookie 的敏感请求头可越过来源边界【合成凭据复现】**

位置：[media_proxy.py:1874](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1874)、[browser_cookies.py:88](/Users/xxd/Playground/video-scrap/src/browser_cookies.py:88)。

HLS 子资源继承头时只比较 hostname，忽略协议和端口。https://example.test/ 的 Authorization 与 X-Token 会被交给 http://example.test:8080/，形成 HTTPS 降级和不同服务间的凭据传播。

另用 requests 的真实重定向准备流程和记录适配器验证：标准 Authorization 跨主机时被移除，但自定义 X-Token 仍会跟随到无关主机。实际泄露的前提是提取器提供了这类敏感自定义头。本次只使用 SYNTHETIC 凭据，没有真实外发。

建议对敏感头比较 scheme、规范主机和有效端口，并在每次重定向重新执行保守白名单。CookieJar 已有 domain/path/secure 约束，不能将此项描述成“所有 Cookie 都会泄露”。

**D06 · P2 · 来源链接 HTML 属性注入，可显示安全域名却打开别的地址【Qt 解析复现】**

位置：[preview_panel.py:553](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:553)，关联 [preview_panel.py:299](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:299)。

url 和 host 未转义就拼入富文本，并启用外部链接。经过 build_items_from_urls() 的候选 https://safe.invalid/a.mp4?x="href="file:///tmp/probe，在 QLabel 对应文本中显示 safe.invalid，实际 anchorHref 却成为 file:///tmp/probe。本次只解析，未点击或打开文件。

建议属性按带引号的 HTML 规则转义，显示文本单独转义；通过 linkActivated 校验最终链接仅为 HTTP(S) 后打开。普通实体字符也会改变 URL；没有证据支持在此处宣称任意 JavaScript 执行。

**D07 · P2 · 预取失败没有退避，持续快速请求失效资源【受控计数复现】**

位置：[media_proxy.py:568](/Users/xxd/Playground/video-scrap/src/media_proxy.py:568)、[media_proxy.py:590](/Users/xxd/Playground/video-scrap/src/media_proxy.py:590)、[media_proxy.py:748](/Users/xxd/Playground/video-scrap/src/media_proxy.py:748)、[media_proxy.py:779](/Users/xxd/Playground/video-scrap/src/media_proxy.py:779)。

顺序预取只在外层异常时等待，内层却吞掉连接异常并对 HTTP 错误直接返回；HLS 失败 future 也被吞掉，下一轮立即重排。403、404、429 或快速连接错误会造成重复请求、CPU/网络消耗和进一步限流。

即时模拟 403 时，顺序路径 0.0007 秒尝试 1,000 次，HLS 路径 0.0012 秒尝试 100 次。这只是无网络模拟计数，用来证明没有等待，不是真实网络吞吐。

建议返回明确的成功/失败/无进展状态，增加可取消的指数退避、Retry-After 和连续失败阈值；鉴权过期应提示重新抓取。

**D08 · P2 · 抓取直链时仍全量读视频并当 HTML 解析【静态确认】**

位置：[scraper.py:487](/Users/xxd/Playground/video-scrap/src/scraper.py:487)、[scraper.py:850](/Users/xxd/Playground/video-scrap/src/scraper.py:850)。

yt-dlp 已成功仍继续 HTML 扫描；requests.get 默认完整读响应，再访问 text、交给 BeautifulSoup。没有类型检查和大小上限。粘贴大 MP4 直链可能在发现阶段完整下载视频并产生额外字符串/解析内存。

建议识别直接媒体后避免无意义页面扫描；HTML 用有限流式读取、响应类型/前缀判断和总大小、总耗时限制。没有为此实际下载大视频或制造 OOM。

**D09 · P2 · 普通 HLS 分片、清单和密钥读取无上限【内存量化复现】**

位置：[downloader.py:1470](/Users/xxd/Playground/video-scrap/src/downloader.py:1470)、[downloader.py:1090](/Users/xxd/Playground/video-scrap/src/downloader.py:1090)、[downloader.py:1410](/Users/xxd/Playground/video-scrap/src/downloader.py:1410)、[media_proxy.py:1242](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1242)。

普通分片先存 chunks 再 join，清单用 text、密钥用 content；缺少独立输入预算。模拟一个 **80 MiB** 分片，函数正常返回，单请求 tracemalloc 峰值 **160.1 MiB**；16 并发会继续放大。持续发送数据也能一直满足读取间隔超时。

建议普通分片直接流式写入私有文件，对清单、AES 密钥和分片分别设限，限制在途任务与绝对时限。PNG 包装解码的 64 MiB 限额只保护对应路径，不能推导其他路径也受保护。本次未真正耗尽内存。

**D10 · P2 · 缩略图无大小/总时间/解码尺寸限制【静态确认】**

位置：[workers.py:89](/Users/xxd/Playground/video-scrap/src/gui/workers.py:89)、[workers.py:96](/Users/xxd/Playground/video-scrap/src/gui/workers.py:96)。

图片请求全量读入，再通过 Qt 信号传给主线程解码；列表与预览合计最多 7 个请求。没有累计字节或图片像素预算，timeout=15 不是整个响应的绝对时限。

建议流式读图、大小和总时限限制、响应类型检查及取消检测，解码前检查尺寸并尽量缩放。未进行内存耗尽试验。

**D11 · P2 · 上游忽略 Range 时，短预取变成整文件下载【离线响应复现】**

位置：[media_proxy.py:600](/Users/xxd/Playground/video-scrap/src/media_proxy.py:600)。

预取发送 Range 后遇到 200，会持续读到 EOF 并缓存全部，没有在目标区间结束时停止。受控请求 bytes=0-1，响应 10 字节，最后缓存 10 字节；对真实大文件会越过预期缓冲、占用带宽和磁盘。

建议完整响应的顺序回退也遵守当前位置及预算；发现不支持 Range 后明确管理顺序读取和取消，避免偷偷下载整部视频。

**D12 · P2 · HLS BYTERANGE 预取没有保留逻辑区间【离线响应复现】**

位置：[media_proxy.py:1832](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1832)、[media_proxy.py:769](/Users/xxd/Playground/video-scrap/src/media_proxy.py:769)、[media_proxy.py:790](/Users/xxd/Playground/video-scrap/src/media_proxy.py:790)。

清单里的 EXT-X-BYTERANGE 被保留给播放器，但预取调度没有区间信息；每个逻辑片段会不带 Range 请求整个共享对象。多个逻辑片段共享同一 Entry 时，首次缓存前还可能重复调度。

受控 2 字节逻辑片段没有发送 Range，缓存了整个 10 字节对象。真实单文件 HLS 可能反复读取大 MP4/TS。

建议片段模型同时保存 URI、起点和长度，按区间预取，并去重在途区间。

**D13 · P2 · 直播滑动窗口的旧片段和文件持续保留【重复刷新复现】**

位置：[media_proxy.py:1858](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1858)、[media_proxy.py:1898](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1898)。

相同 URL 的重用只防止重复创建，不能清理已经离开直播窗口的不同 URL。100 次刷新、当前窗口仅 3 片，仍保留 **102 个子条目和 103 个缓存文件**。文件数据也会保留至整个视频释放。

普通长视频已经读过的区间同样没有累计磁盘额度；缓冲秒数只是读前目标，不是缓存总量。并发查找后注册还不是原子操作，有重复子条目的窗口。

建议设置缓存字节/剩余空间预算，直播按序列淘汰，给在途播放器请求保留短宽限期，并将查找/创建做成原子流程。

**D14 · P2 · GUI 同步等待并强停抓取/缩略图线程【隔离复现】**

位置：[video_list.py:175](/Users/xxd/Playground/video-scrap/src/gui/video_list.py:175)、[preview_panel.py:777](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:777)、[main_window.py:1756](/Users/xxd/Playground/video-scrap/src/gui/main_window.py:1756)。

同步 GET 不检查中断；没有线程事件循环时 quit 也不能结束该请求。GUI 先 wait，再 terminate，列表可逐个累加等待。

隔离子进程里模拟 GET 持锁并等待，清空列表阻塞 **1.005 秒**；强停后 with 清理没执行、锁仍保持。真实网络库持锁时有同类资源风险，但本次没有证明真实库必然死锁。

建议协作取消、有限读取；切换先使旧结果失效，线程在后台正常结束并通过 finished 回收。不能删除仍运行的 QThread。下载监督线程已有独立进程机制，不应笼统说所有下载取消都用 terminate。

**D15 · P2 · 异常高度经真实提取器返回后，预览选流抛未捕获异常【真实库离线夹具】**

位置：[preview_panel.py:730](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:730)，X 选流同类位置 [preview_panel.py:600](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:600)。

int(height) 只捕获 TypeError、ValueError，没有捕获 OverflowError 或预先验证有限数值。

使用已安装 yt-dlp 的真实 GabIE，仅将远端 JSON 替换为离线夹具：meta.original.height 为 Infinity，width 缺省。通过真实 extract_info 和 VideoScraper._scrape_with_ytdlp 后，formats.height 仍为 Infinity；预览选流/展示抛 OverflowError。不是只手工构造一个畸形 VideoItem。未把“Qt 槽异常会在所有系统必然终止进程”当作已验证结论。

建议在数据模型边界规范化有限、非负的尺寸/码率，选流也防御转换失败；异常条目应降级为未知尺寸，避免使结果展示失败。

**D16 · P2 · 浏览器抓取的 X 直链被错误禁用预览【组件流程复现】**

位置：[preview_panel.py:407](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:407)、[preview_panel.py:623](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:623)。

show_video 先按 X 来源选择 formats，才判断直链。真实 build_items_from_urls 对 X 帖子的 video.twimg.com/a.mp4 产生 is_direct=True、formats=[]；_best_playable_url 返回有效 URL，但界面 pending 为 None、Play 禁用。

X 的代理分支还只传 x_cookiejar，没有传交互浏览器的 browser_cookies。修复直链分支时应同步统一会话传递，否则受保护媒体仍可能失败；后一个失败场景未对真实登录站点实测。

建议先处理直链，再进入 X 提取格式选择，并统一代理会话参数。

**D17 · P3 · 旧缩略图排队回调覆盖新选择【Qt 队列复现】**

位置：[preview_panel.py:400](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:400)、[preview_panel.py:745](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:745)、[preview_panel.py:773](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:773)。

A 已结束、ready 尚排队时切到 B，停止函数因线程不再运行直接返回，随后 A 的回调无条件改 B 图片。已复现 B 标题正确却显示 A 的测试图；旧 failed 也有相同问题，不涉及下载字节串台。

建议预览每次选择增加 generation/worker 身份校验，切换、清空和关闭时失效。列表已有代次保护，应保留。

**D18 · P3 · 已完成缩略图线程对象随使用次数累积【QObject 检查复现】**

位置：[video_list.py:112](/Users/xxd/Playground/video-scrap/src/gui/video_list.py:112)、[video_list.py:178](/Users/xxd/Playground/video-scrap/src/gui/video_list.py:178)、[preview_panel.py:393](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:393)。

线程以 widget 为 Qt parent，完成后未 deleteLater。清空 Python 列表/替换引用不会删除父对象持有的 Qt 子对象。

连续 10 次加载并处理 DeferredDelete，组件仍持有 **10 个 ThumbnailWorker**，当前列表只跟踪 1 个。对象、URL 和相关会话引用会一直留到组件销毁。

建议 finished 时移出跟踪集合再 deleteLater，保留仍运行线程的所有权。

**D19 · P3 · 畸形 Next.js 结构阻断正常 TanStack 回退【本地页面夹具复现】**

位置：[scraper.py:289](/Users/xxd/Playground/video-scrap/src/scraper.py:289)、[scraper.py:305](/Users/xxd/Playground/video-scrap/src/scraper.py:305)、[scraper.py:613](/Users/xxd/Playground/video-scrap/src/scraper.py:613)。

JSON 语法合法不代表嵌套类型正确。根为 []、props 为字符串、pageProps 为整数都会在 get 处抛异常；共用的外层 try 使后续 TanStack 提取也被跳过。

上述三种输入后附有效 TanStack 播放器，均得到零结果并记录 AttributeError。建议逐层检查映射类型，每种提取器独立容错，失败后继续回退。

**D20 · P3 · HLS 预取位置只增不减，回退播放仍预取旧位置【状态复现】**

位置：[media_proxy.py:1149](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1149)。

请求第 10 片再请求第 1 片，锚点仍是 10。直播窗口重排下标也可能失配。播放器仍可按需取旧片，因此不能据此宣称一定无法播放。

建议区分最新播放位置和历史最高位置，直播使用绝对序列并重映射；避免把预取/探测请求当成真实播放推进。

**D21 · P3 · HTTP 200 登录页被改写成成功 HLS 清单【响应复现】**

位置：[media_proxy.py:1274](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1274)。

m3u8 扩展名足以通过普通清单判断；200 text/html 的 Login required 会逐行转成新的代理片段 URL，并以 200 application/vnd.apple.mpegurl 返回。既掩盖登录失败原因，也创建无意义资源。

建议检查 #EXTM3U 和必要结构，错误正文应产生可解释的失败；扩展名和 Content-Type 只能作为线索。

**D22 · P3 · SettingsStore 并发更新时，内存、磁盘及通知可互相矛盾【确定性交错复现】**

位置：[settings.py:154](/Users/xxd/Playground/video-scrap/src/settings.py:154)。

锁只保护内存，save 和回调在锁外。先启动写 10，再写 20，控制落盘先后后，结果为 **内存 20、磁盘 10、通知顺序 [20, 10]**。最后启动的更新并未决定最终持久化值。

当前主界面主要同线程更新，因此这是公开“线程安全”接口的真实缺口，尚未确认普通点击导致了设置丢失。建议序列化整个更新事务或增加版本号/单写入队列，并明确回调执行线程。

**D23 · P3 · 无硬链接发布回退存在第三方替换目标的覆盖竞态【确定性交错复现】**

位置：[parallel_downloader.py:92](/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:92)。

O_EXCL 创建占位文件后关闭，再 os.replace；这不能保证路径仍是自己的占位文件。模拟硬链接不可用，在替换前第三方删除占位并写入新内容，随后新内容被覆盖，函数仍成功。

正常两个本应用下载之间仍有独占命名保护；本项需要其他程序/进程同时改目标路径，不能泛化成“同名下载总覆盖”。

建议采用平台原子禁止覆盖发布能力，或保留独占描述符并重新设计回退。简单替换前 stat 仍有检查/使用间隙。

**三、条件性风险、接口约束与尚待验证的边界**

| 编号 | 位置 | 当前事实、影响边界及建议 |
|---|---|---|
| R01 · P2 | [media_proxy.py:741](/Users/xxd/Playground/video-scrap/src/media_proxy.py:741)、[media_proxy.py:757](/Users/xxd/Playground/video-scrap/src/media_proxy.py:757) | 向前 seek 后只 break 等待，不取消旧 future，也无在途标记；下一轮继续提交。全局 8 线程只限正在执行数量，不限排队数量。慢预取期间多次跳转可能下载旧窗口并拖慢当前位置。代码路径确认，未做长时积压压力试验。应增加代次、在途区间和有界 outstanding 队列。 |
| R02 · P2 | [media_proxy.py:206](/Users/xxd/Playground/video-scrap/src/media_proxy.py:206)、[media_proxy.py:900](/Users/xxd/Playground/video-scrap/src/media_proxy.py:900)、[media_proxy.py:771](/Users/xxd/Playground/video-scrap/src/media_proxy.py:771) | 全局 HLS executor 和活动响应不由 stop 完整拥有/取消；关闭 Session 不能据此证明阻塞 read 已中断。工作线程可能延迟解释器退出。尚未量测真实卡住源的退出上限，应补独立进程停机测试并显式关闭响应、取消待执行任务。 |
| R03 · P2 | [media_proxy.py:831](/Users/xxd/Playground/video-scrap/src/media_proxy.py:831)、[download_job.py:510](/Users/xxd/Playground/video-scrap/src/download_job.py:510) | 强杀、崩溃、断电可留下 video_scraper_cache_* 和 .video-scrap-job-*；启动没有应用级失活识别/回收。正常完成和取消已有清理，不属于此问题。建议所有者/存活标记与保守回收或可见清理入口，不能按前缀盲删其他运行任务。 |
| R04 · P3 | [download_job.py:579](/Users/xxd/Playground/video-scrap/src/download_job.py:579)、[download_job.py:596](/Users/xxd/Playground/video-scrap/src/download_job.py:596) | 子进程创建 FFmpeg 后立即异常退出，可能早于约 30ms 的后代发现，未记录后代被重挂后难以再发现。已知后代的取消测试通过，不证明此崩溃窗口不存在。静态风险，未制造并声称复现孤儿；评估进程组/Windows Job Object 等归属机制。 |
| R05 · P3 | [download_job.py:339](/Users/xxd/Playground/video-scrap/src/download_job.py:339)、[download_job.py:351](/Users/xxd/Playground/video-scrap/src/download_job.py:351) | 状态写入后解锁，再 wake 和回调；并发控制时旧通知可能后到。受控 API 交错得到通知顺序 paused、pausing，而实际状态已是 paused。GUI 已有多处旧状态保护，没有证据证明普通按钮因此卡死。建议统一事件序列或状态编号，不能简单把任意外部回调挪到锁内。 |
| R06 · P2/平台条件 | [utils.py:47](/Users/xxd/Playground/video-scrap/src/utils.py:47) | CON、NUL、COM1、LPT1 被原样保留；加 .mp4 后 PureWindowsPath 仍认定保留名。还没有 Windows 本机创建证据。120 字符截断也不保证所有文件系统的字节/总路径限制；本机 APFS 成功创建 120 汉字+.mp4，不能声称当前 Mac 中文标题必然过长。应处理保留名、尾随点/空格，并在目标文件系统验证。 |
| R07 · P2/输入规模条件 | [js_decoder.py:101](/Users/xxd/Playground/video-scrap/src/js_decoder.py:101) | 每个字符串数组反复扫描全局候选，随数组数超线性增长。200/400/800/1600 对，约 10/21/42/86 KB，测得 0.022/0.046/0.145/0.491 秒。只作规模趋势证据，未制造卡死。应按脚本分区、邻近索引和候选/输入预算处理。 |
| R08 · P3/接口复用 | [js_renderer.py:96](/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py:96)、[js_renderer.py:136](/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py:136) | 同一实例 scrape(A) 后 scrape(B)，A 的旧 JS 回调在 _done 重置后可结束 B、删除 B 页面。FakePage 已复现，但 MainWindow 当前每次新建实例，不是当前主界面已发生的任务串台。建议每次调用使用独立 token，或明确禁止复用。 |
| R09 · P3/分享日志条件 | [interactive_scraper.py:300](/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:300)、[main_window.py:769](/Users/xxd/Playground/video-scrap/src/gui/main_window.py:769) | 捕获的完整媒体 URL 进入可见日志，可能包含短效签名/token；用户分享日志或截图时会暴露。没有自动上传日志的证据。建议默认遮蔽敏感查询参数，提供明确的完整调试选项。 |
| R10 · P3/生命周期约束 | [settings.py:170](/Users/xxd/Playground/video-scrap/src/settings.py:170)、[media_proxy.py:847](/Users/xxd/Playground/video-scrap/src/media_proxy.py:847) | subscribe 没有取消/去重机制，保存 bound method 可延长订阅者寿命。当前代理通常与应用同寿命，没有证明普通使用持续泄漏。建议返回取消订阅句柄，组件停止时解除。 |
| R11 · P3/并发设计约束 | [net.py:9](/Users/xxd/Playground/video-scrap/src/net.py:9)、[workers.py:89](/Users/xxd/Playground/video-scrap/src/gui/workers.py:89) | 连接池并发能力不能等价为 Session 所有可变状态可任意并发。代码的线程安全说明过于绝对；本次未证明 CookieJar 或正文发生真实竞态。应明确默认 headers/auth/cookies 不得并发修改，必要时隔离会话状态。 |
| R12 · P3/缺可选组件 | [main_window.py:584](/Users/xxd/Playground/video-scrap/src/gui/main_window.py:584)、[main_window.py:991](/Users/xxd/Playground/video-scrap/src/gui/main_window.py:991) | 以 InteractiveScrapeDialog 类是否为 None 判断浏览器能力；WebEngine 缺失时类仍可能被导出，直到构造才失败，因此降级界面还展示/建议不可用入口。当前 requirements 已要求 WebEngine，属于不完整安装的降级路径。应统一读取实际 availability 标记。 |

**四、结构、构建及维护建议**

这些项目帮助降低未来风险，不等于已发生的下载故障；不应为凑数量把它们都包装成严重 Bug。

| 编号 | 位置 | 发现及建议 |
|---|---|---|
| M01 | [requirements.txt:1](/Users/xxd/Playground/video-scrap/requirements.txt:1) | 多数运行依赖只有下限，不能复现已验收的完整版本组合；两个打包工具固定不代表全环境固定。建议另保留发布锁定清单/哈希并有计划升级。没有进行第三方漏洞全量扫描，未凭版本号推断存在 CVE。 |
| M02 | [tests/](/Users/xxd/Playground/video-scrap/tests) | unittest 类和普通函数入口混用；直接 unittest discover 会漏函数测试，Qt 全局初始化还可能冲突。本次已有合并运行异常及分进程成功的对照。建议提供版本控制内的统一隔离测试入口和真实执行数量，不能把“模块导入成功”计作全部检查通过。 |
| M03 | [README.md:252](/Users/xxd/Playground/video-scrap/README.md:252)、[WINDOWS_BUILD.md:101](/Users/xxd/Playground/video-scrap/WINDOWS_BUILD.md:101) | 未看到版本控制内的 CI；当前源码的 Windows 原生任务控制、NTFS、Qt 播放、新成品仍缺本轮报告。mock 平台分支不能替代 Windows。建议 macOS/Windows 矩阵和打包后离线检查；现有文档对 Windows 验证状态的措辞也宜统一。 |
| M04 | [build_macos.py:150](/Users/xxd/Playground/video-scrap/scripts/build_macos.py:150) | app 和 ZIP 已生成后无条件运行 git rev-parse/status；源码 ZIP 或缺 Git 环境会在记录阶段报错，Windows git_state 已有容错。静态确认，未重新打包复现。建议统一成可缺失的 Git 元数据，不把成品产出与提交信息可用性混为一体。 |
| M05 | [VideoScraper-Windows.spec:40](/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper-Windows.spec:40) | EXE 未配置 VERSIONINFO。About 使用正确版本，不代表系统文件属性中也有版本。建议从单一版本源生成资源；没有检查 Windows 成品属性，属于配置缺口。src、Mac spec、README 当前 0.3.2 一致。 |
| M06 | [main_window.py:137](/Users/xxd/Playground/video-scrap/src/gui/main_window.py:137)、[main_window.py:1460](/Users/xxd/Playground/video-scrap/src/gui/main_window.py:1460) | 保留 lane limiter 字段及兼容调用，但独立下载进程路径不再由这套对象实际统一调度，容易让维护者误解总限流。应查清外部调用后明确废弃或接通；不能宣称当前“每任务最多 16”意味着全应用最多 16。 |
| M07 | [downloader.py](/Users/xxd/Playground/video-scrap/src/downloader.py)、[media_proxy.py](/Users/xxd/Playground/video-scrap/src/media_proxy.py)、[main_window.py](/Users/xxd/Playground/video-scrap/src/gui/main_window.py) | 分别 2,038、1,901、1,767 行，多个职责在同一文件中交织；质量、会话和完整性规则容易在后端间不一致。建议先建立统一验收契约和清单/片段模型，再逐步拆传输、缓存、选流、任务状态和视图。行数本身不是缺陷，也不建议为重构而大规模重写。 |

**五、已有效的保护与排除的误报**

- PNG 包装路径已有 CRC、块边界、解压后大小和尾部数据保护；本轮没有发现新的确定性解包问题。
- Byte-Range 的区间/长度检查、反转 Range 拒绝、压缩 wire bytes 转发已有测试保护。D04 是资源版本一致性缺口，不是声称这些长度保护都无效。
- 下载使用私有暂存、结果路径/符号链接校验和提交阶段保护；正常并发同名下载保留已有文件，取消/暂停/清理的现有回归通过。D23 是第三方替换占位的窄条件。
- 浏览器 Cookie 的 host-only、domain、path、secure、expiry 及跨主机重定向已验证；D05 针对非 Cookie 的敏感自定义头。
- 旧抓取结果覆盖新任务、重定向/base、blob/分片候选、预览文件名误过滤、设置损坏恢复/保存错误提示、JS/交互页面释放等先前修复仍有回归保护，没有将旧报告 B04–B26 整体照搬为当前未修复问题。
- About、Preferences 和完成的普通抓取 worker 当前已有 deleteLater 路径；不能继续沿用旧报告中它们全部未释放的结论。D18 针对剩余缩略图对象。
- format_duration(NaN/Infinity) 的纯函数确实抛异常，但当前真实 yt-dlp 会在返回前拒绝相应 duration。本轮不把它写成已经证明可从真实网页触发的 GUI 时长崩溃；D15 的高度值有独立真实提取器证据。
- 没把 Qt 请求拦截器的直接 UI 调用臆断为当前必然跨线程 bug，也没把沙箱 Chromium 启动拒绝写成应用缺陷。
- 直接 DASH 应用内预览目前明确不支持，下载已分流；受限 JS 静态解码也不等于完整 JS 解释器。已明示的能力范围不重复计为新缺陷。
- 本次是源码验证，frozen=false；不会据此认定 dist 中已有安装包包含当前未提交修复或通过了重新打包验证。

**六、逐文件审查清单**

| 文件 | 核对重点及结果 |
|---|---|
| [main.py](/Users/xxd/Playground/video-scrap/main.py) | Qt/单实例顺序、freeze_support、事件循环退出后的下载监督清理；现有退出检查通过，无新增独立确定性缺陷。 |
| [src/__init__.py](/Users/xxd/Playground/video-scrap/src/__init__.py) | 版本 0.3.2，与 Mac spec、README 一致。 |
| [browser_cookies.py](/Users/xxd/Playground/video-scrap/src/browser_cookies.py) | 严格 Cookie 范围和每次 send；D05 自定义头边界，Cookie 本身未发现新增确定性缺陷。 |
| [download_job.py](/Users/xxd/Playground/video-scrap/src/download_job.py) | IPC 包、状态、进程树、暂停/取消、暂存路径和成品提交；R03–R05。 |
| [downloader.py](/Users/xxd/Playground/video-scrap/src/downloader.py) | 直链/yt-dlp/HLS/音频所有路由、fallback、校验和发布；D01–D03、D09，关联 D04/D23。 |
| [ffmpeg.py](/Users/xxd/Playground/video-scrap/src/ffmpeg.py) | PATH、frozen、本地资产、imageio 查找和平台边界；18 项现有测试通过，无新增确定性缺陷。 |
| [hls_payload.py](/Users/xxd/Playground/video-scrap/src/hls_payload.py) | PNG/roUd、CRC、压缩、限制及错误路径；18 项现有测试通过，无新增确定性缺陷。 |
| [js_decoder.py](/Users/xxd/Playground/video-scrap/src/js_decoder.py) | 字面量、遮罩、变量操作链和 JS replace 语义；15 项现有回归通过，R07 规模风险。 |
| [media_proxy.py](/Users/xxd/Playground/video-scrap/src/media_proxy.py) | Entry 锁、HTTP/Range、清单重写、缓存、预取、会话和释放；D04/D05/D07/D09/D11–D13/D20/D21、R01–R03/R10。 |
| [net.py](/Users/xxd/Playground/video-scrap/src/net.py) | 连接池和共享 Session 约束；R11。 |
| [parallel_downloader.py](/Users/xxd/Playground/video-scrap/src/parallel_downloader.py) | 探测、分段、短写、长度、fsync、保留名称和发布；D03/D04/D23。 |
| [scraper.py](/Users/xxd/Playground/video-scrap/src/scraper.py) | X/通用 yt-dlp、HTML、URL/base、配置/Next/TanStack、浏览器结果；D08/D19，D15 的数据入口。 |
| [settings.py](/Users/xxd/Playground/video-scrap/src/settings.py) | 归一化、损坏备份、原子替换、错误传播和监听；D22、R10。 |
| [single_instance.py](/Users/xxd/Playground/video-scrap/src/single_instance.py) | 锁、陈旧 socket、并发启动、激活协议和 foreground；12 项跨进程回归通过，无新增确定性缺陷。 |
| [utils.py](/Users/xxd/Playground/video-scrap/src/utils.py) | URL 初筛、时长/字节显示、文件名；R06；时长纯函数的可达性限制见第五部分。 |
| [gui/__init__.py](/Users/xxd/Playground/video-scrap/src/gui/__init__.py) | 包初始化，无新增问题。 |
| [about_dialog.py](/Users/xxd/Playground/video-scrap/src/gui/about_dialog.py) | 版本/图标/外链/布局和关闭；无新增独立缺陷。 |
| [icons.py](/Users/xxd/Playground/video-scrap/src/gui/icons.py) | SVG 生成、颜色与图标映射；无新增独立缺陷。 |
| [interactive_scraper.py](/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py) | profile/cookie 观察、媒体过滤、拦截器、页面释放；R09，关联 D16。 |
| [js_renderer.py](/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py) | 页面加载/超时、JS 回调、快照和释放；R08，生命周期既有测试通过。 |
| [main_window.py](/Users/xxd/Playground/video-scrap/src/gui/main_window.py) | 抓取代次、所有入口、目录恢复、队列/槽位、任务控制、布局和退出；D14、R12、M06/M07。 |
| [preview_panel.py](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py) | 选流、懒加载、代理会话、滑条/音量/全屏、元数据/缩略图；D06/D14–D18。 |
| [settings_dialog.py](/Users/xxd/Playground/video-scrap/src/gui/settings_dialog.py) | 输入提交、保存失败与重试、主题保留；无新增独立缺陷。 |
| [style.py](/Users/xxd/Playground/video-scrap/src/gui/style.py) | 主题、palette 和控件样式；无新增独立逻辑缺陷，实际布局既有测试通过。 |
| [video_list.py](/Users/xxd/Playground/video-scrap/src/gui/video_list.py) | 列表选择、缩略图限流/代次和回收；D14/D18。 |
| [workers.py](/Users/xxd/Playground/video-scrap/src/gui/workers.py) | 抓取/图片/独立任务监督和信号；D10/D14、R11。 |
| [build_macos.py](/Users/xxd/Playground/video-scrap/scripts/build_macos.py) | 资产、许可证、系统最低要求、签名检查、归档和哈希；M01/M04，未重打包。 |
| [build_windows.py](/Users/xxd/Playground/video-scrap/scripts/build_windows.py) | x64/PE 检查、ICO、FFmpeg、归档和构建报告；无新增确定性运行缺陷，Windows 验证仍缺。 |
| [smoke_app.py](/Users/xxd/Playground/video-scrap/scripts/smoke_app.py) | 本地夹具、15 项端到端检查及收尾；源码运行通过，不能替代新成品检查。 |
| [runtime_hook.py](/Users/xxd/Playground/video-scrap/app_bundle/runtime_hook.py) | bundled FFmpeg PATH；无新增独立缺陷。 |
| [VideoScraper.spec](/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper.spec) | 资源、hidden imports、版本和最低 macOS；无新增独立确定性缺陷。 |
| [VideoScraper-Windows.spec](/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper-Windows.spec) | Windows 资源/hidden imports；M05。 |

全部 38 个测试文件、用例数、退出结果和日志路径见测试汇总；下载、抓取/代理、GUI 分组报告进一步记录阅读范围和新增复现。测试验证的是自己的断言，不意味着每条源码路径都具有覆盖率证明。

**七、修复顺序和验收方式**

1. 先修 D01–D06、D15：统一媒体成品完整性，按轨道验证，绑定资源版本，收紧凭据和外链边界，拒绝导致选流异常的尺寸值。验收应包含真实 FFmpeg 输出和真实库数据边界，不能只 mock “验证函数被调用”。
2. 再修 D07–D14 及 R01/R02：给网络、内存、磁盘、在途队列和关闭时间设置实际预算；失败源要退避，缓存和旧 future 要能退出。用快速失败源、忽略 Range 源、滑动直播和慢响应夹具验证。
3. 修 D16–D23：直链分流和会话、缩略图代次/回收、提取器独立回退、设置事务与发布竞态；保留已有正常交互和取消行为。
4. 最后逐步清理维护问题并做 Windows 与新安装包验收。源码测试通过后仍需重新构建才能交付修复；只有用户授权推送时，才按项目规则为该批次递增版本。

**八、证据目录**

- [下载复核明细](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/download-review.md)及同目录下载复现脚本/输出。
- [抓取与代理复核明细](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/scraping-proxy-review.md)及同目录夹具/输出。
- [GUI 复核明细](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/gui-review.md)及同目录 Qt 复现脚本。
- [设置和文件名诊断](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/root-probe-output.json)。
- [38 文件测试结果](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/final-test-results.json)。
- [15 项源码集成检查](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/source-smoke.json)。
- [源码基准及哈希](/Users/xxd/Playground/video-scrap/build/audit-2026-10-18/source-manifest.json)。

build 目录被 Git 忽略，证据不会自动成为提交内容；本报告单独保存在项目根目录，旧审查报告保留原样。
