# R01–R09 修复与最终验证记录

日期：2026-10-22，Asia/Kuala_Lumpur（UTC+08:00）。

## 1. 范围与结果

本轮按用户“仔细调研，确认后开始修复”的授权，处理 `code_audit_current_2026-10-19.md`、`code_audit_recheck_2026-10-20.md` 中重新复现的 9 项问题。基点为 `b8cf63298e592afcda8e88520e490610942937dc`，源码版本仍为 `0.3.4`。

**R01–R09 的已确认触发路径均已修复。最终完整回归为 57 个模块、611 项执行，0 失败、0 错误、0 跳过。** 本轮同时补强了暂停采样交错、SPA 重扫与固定直播窗口结束这些相邻边界。结论限定于代码和证据覆盖的行为，不等于整个项目已不存在其他缺陷。

当前改动留在工作区，没有提交、推送、更新版本或生成安装包。以前的审查和修复记录保留原样。

## 2. 逐项修复与依据

### R01：HLS 后备下载重新获取变化后的清单

原问题：首次清单没有范围标签，原生请求失败后，其他后端重新获取相同 URL 的新清单；上游忽略 Range 时，原预期 2 秒内容可被重复拼成 4 秒仍报成功。

现在把本次已经读取并检查的清单保存为任务快照，后备 yt-dlp/FFmpeg 只访问任务私有的回环地址。需要使用主清单和外置音轨时，全部可能引用的子清单也必须已经检查。普通视频回退只暴露选中的媒体清单，避免无关变体影响当前选择。

媒体、密钥、初始化段通过独立的限时会话获取；同一路由完整验证后写入临时文件，后续 Range、HEAD、重试都使用该份表示，避免不同请求拼接不同正文。上游地址必须是 HTTP(S)，不能借本地快照访问 file/data 协议或未检查的清单。原范围清单仍由已有原生范围校验路径处理。

对于没有 ENDLIST 的媒体清单，快照明确终止于本次已检查的可用窗口，与原生下载当前窗口的行为一致，避免 FFmpeg 无限重载一份不再变化的直播清单。退出时关闭专用会话和客户端连接，等待处理线程结束并清理临时文件。

- 位置：`/Users/xxd/Playground/video-scrap/src/downloader.py:1103`、`:1137`、`:1189`。
- 新模块：`/Users/xxd/Playground/video-scrap/src/hls_snapshot.py:42`。
- 真实正向复现：上游先 503、同 URL 之后会变为 Range 清单，修复后只读取原清单一次，仍能回退完成正确的 2 秒成品，不请求后来变化的对象。
- 另外验证：实际 FFmpeg 回退、加密回退、同一路由 Range 复用、拒绝本地文件/未检查清单、慢客户端关闭和有限直播窗口退出。
- 预算：快照清单总计 16 MiB、最多 1,024 份；最多 200,000 个资源路由；4 个并行资源处理槽；每个媒体资源最多 64 MiB。后备资源暂存占用磁盘，任务结束清理，不是持久播放缓存。

### R02：暂停时间消耗 HLS 读取期限

原问题：OS 挂起下载进程时，monotonic 总期限仍流逝；恢复后正常数据被判超时。

现在每个下载任务使用独立的共享暂停偏移。监督器在恢复任何工作进程前计入暂停时长，子进程的 HLS 正文、整对象和响应头期限使用扣除暂停后的活动时间。共享值由监督器单方写入，读者不获取跨进程锁，避免在工作进程被挂起时阻塞恢复。

交叉检查还发现“先读时钟、暂停、再读新偏移”会产生错误的旧时间减新偏移。本轮改为前后读取偏移并核对，变化则重新采样。默认非下载会话继续使用普通计时器，已有响应头/连接归属保护保留。

- 位置：`/Users/xxd/Playground/video-scrap/src/transfer_clock.py:21`、`:35`；`/Users/xxd/Playground/video-scrap/src/download_job.py:570`；`/Users/xxd/Playground/video-scrap/src/browser_cookies.py`、`/Users/xxd/Playground/video-scrap/src/http_limits.py`。
- 真实验证：分别在响应头和正文等待期间暂停，暂停长于缩短后的应用期限，恢复后均完成；不暂停且真正停止交付仍失败。
- 另验证：底层 socket 在暂停期间超时，数据在恢复后才到达，当前段通过重新请求完成；不会把真实 socket 的墙钟超时宣称成可以永久暂停。
- 边界：测试用 0.3 秒 HLS 预算、约 0.5 秒暂停；没有实等生产 120 秒。连接或签名地址在服务端过期时，仍可能需要重新请求或重新抓取。共享标量方案对应当前受支持的 64 位桌面构建。

### R03：JS 替换后的字符串可突破预算

原问题：初始候选受限，后续 `$'` 等替换展开可生成超过上限的大字符串。

现在先计算替换结果长度，再创建切片、展开和拼接结果；初始 join、URI 解码和每个表达式都受预算约束。单候选及中间值最多 64 KiB 字符，单页累计候选最多 2 MiB 字符，转换工作预算为 16 MiB 字符量。某个候选超限时继续检查其他合法块；全页工作预算耗尽时停止后续计算并保留已完成结果。

- 位置：`/Users/xxd/Playground/video-scrap/src/js_decoder.py:195`、`:333`。
- 验证：旧 10 次扩张输入被拒绝，后续合法 URL 保留；可能分配约 65 MiB 的替换在分配前被拒绝，探针新增分配峰值低于 2 MiB；合法美元替换、编码顺序、边界长度和去重预算均通过。
- 限制单位是字符及转换工作量，不是应用总内存的精确承诺。

### R04：交互浏览器跨页混入旧素材和错误来源

现在按顶层页面代次隔离捕获。网络事件携带其 first-party 页面，只接收当前页面来源；当前页面的跨域 iframe/CDN 媒体仍被保留。导航开始时清空旧数据，异步 DOM 回调同时校验页面代次、页面 URL 和抽取序号，较旧回调不能覆盖较新结果。

DOM 集合与网络集合分开保存：重新扫描会替换旧 DOM 结果，当前导航的网络素材保留。这处理了 SPA 先改 URL、延后换视频时，旧 DOM 被永久累加的问题。同址刷新、重定向及 fragment 变更分别验证；HTTP Referer 移除 fragment。

- 位置：`/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:311`、`:328`、`:375`、`:392`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py`。
- 实际 Qt/Chromium 验证：旧页→新页只导入新素材；迟到旧回调被忽略；重定向、跨端口 iframe、同 URL 刷新中的纯网络素材正常；SPA 完成新 DOM 后重扫不再保留旧 DOM 视频。
- 无法确定页面来源的网络事件不再自动归给当前页面，当前 DOM 提取仍可使用。没有使用真实登录 Cookie 验收所有站点。

### R05：清单正文网络异常直接断连接

普通清单正文读取补充捕获预期的 Requests 传输异常，返回有限长度的 502 并写入应用状态日志；原 finally 中的响应释放保留。响应头获取失败也统一使用同一错误输出。错误文本在编码前限制为 1,024 字符，并替换无法编码的字符，保证 Content-Length 与实际字节一致。

- 位置：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1684`、`:1724`。
- 真实回环验证：截断及错误 gzip 均返回 502、有日志、无残留响应；正常 plain/gzip 清单仍重写；取消阻塞读取、HEAD 错误响应和 UTF-8 长度通过。

### R06：键盘/滚轮进度操作没有实际跳转

进度条的用户 action 现在连接到 seek，使用已经更新的 sliderPosition；正在鼠标拖动时避免重复调用，播放器回传产生的程序性 setValue 不反向触发 seek。保留原有 64 位媒体位置映射和全屏控件复用。

依据 Qt 对 actionTriggered 顺序的说明，动作信号发出时 sliderPosition 已更新，而 value 尚未传播，因此这里读取前者。[Qt QAbstractSlider 官方文档](https://doc.qt.io/qt-6/qabstractslider.html#actionTriggered)

- 位置：`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:915`。
- 实测：真实本地 WAV 的 End、Home、PageUp 和滚轮改变真实 QMediaPlayer 位置；程序性进度更新不引起播放跳转。既有鼠标/超长时长/全屏检查通过。

### R07：非法 AES 填充仍发布成功

原生路径现在严格拒绝无效 PKCS#7 和只有填充、没有媒体字节的内容。后备资源层也在交付密文前检查 CBC 最后一个块，校验失败记为不可绕过的完整性错误，不能再通过另一个后端发布。正常加密分片仍完成。

HLS 的 AES-128 模式规定使用 CBC 与 PKCS#7 填充；严格检查适用于该模式，但不等于提供内容认证。[RFC 8216 §4.3.2.4](https://www.rfc-editor.org/rfc/rfc8216#section-4.3.2.4)

- 位置：`/Users/xxd/Playground/video-scrap/src/downloader.py:1174`、`:2428`；`/Users/xxd/Playground/video-scrap/src/hls_snapshot.py:246`。
- 验证：原生非法填充不调用其他后端；原生失败后遇非法填充或 padding-only 密文，也不会进入最终 FFmpeg 后备或发布输出；有效 AES 回退可解码。

### R08：巨大整数声明阻断其他隐藏源

整数数组按排列索引范围预先拒绝异常 token，单声明捕获 ValueError 并继续扫描。没有调整 Python 全局整数限制。

- 位置：`/Users/xxd/Playground/video-scrap/src/js_decoder.py:97`。
- 验证：无关 5,000 位整数前后的合法隐藏 URL 都保留；实际 scrape 同时返回普通视频和隐藏 HLS，不再出现整次 decoder 失败。

### R09：同一媒体跨策略重复显示

提取器已提供的精确格式 URL 纳入去重；后续 HTML/隐藏源不会再单独列出同一格式。嵌入页是在 HTML 后解析，因此也补充了反向合并：保留嵌入页的完整元数据，移除前面同 URL 的普通 direct 副本。保留不同 query、独立视频和全部清晰度；具有额外包装解码语义的条目不按普通副本移除。

- 位置：`/Users/xxd/Playground/video-scrap/src/scraper.py:541`、`:597`。
- 验证：主页面/嵌入页格式与 HTML 重叠都去重，元数据与格式列表保留；另一视频及不同 query 不被误合并。

## 3. 代码与测试文件

| 完整路径 | 变更摘要 |
|---|---|
| `/Users/xxd/Playground/video-scrap/src/hls_snapshot.py` | 新增不可变清单、受验证资源及任务私有临时文件服务。 |
| `/Users/xxd/Playground/video-scrap/src/transfer_clock.py` | 新增暂停补偿时钟、一致采样和活动时间计时器。 |
| `/Users/xxd/Playground/video-scrap/src/downloader.py` | 固定清单回退、资源预算、严格 AES 校验及 HLS 活动时间。 |
| `/Users/xxd/Playground/video-scrap/src/download_job.py` | 为每个任务建立暂停偏移，在恢复前补偿。 |
| `/Users/xxd/Playground/video-scrap/src/browser_cookies.py` | 下载子进程响应头使用补偿后的计时器。 |
| `/Users/xxd/Playground/video-scrap/src/http_limits.py` | 正文期限及进展判断使用任务活动时间。 |
| `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py` | 当前页面/抽取代次、来源校验及最新 DOM 结果替换。 |
| `/Users/xxd/Playground/video-scrap/src/gui/main_window.py` | 导入媒体的 Referer 移除 fragment。 |
| `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py` | 键盘/滚轮 seek，避免鼠标与程序性回传重复触发。 |
| `/Users/xxd/Playground/video-scrap/src/js_decoder.py` | 分配前大小/工作预算及整数声明容错。 |
| `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | 正文 Requests 异常处理及受限错误响应。 |
| `/Users/xxd/Playground/video-scrap/src/scraper.py` | 主页面和嵌入页格式 URL 去重。 |
| `/Users/xxd/Playground/video-scrap/tests/test_hls_fallback_snapshot.py` | 新增 10 项固定清单、资源重试、加密、退出及真实后备检查。 |
| `/Users/xxd/Playground/video-scrap/tests/test_transfer_pause_boundaries.py` | 新增 6 项时钟交错、实际暂停、真正断流及 socket 重试检查。 |
| `/Users/xxd/Playground/video-scrap/tests/test_decoder_resource_limits.py` | 新增 12 项分配预算、JS 语义与整数容错检查。 |
| `/Users/xxd/Playground/video-scrap/tests/test_proxy_manifest_errors.py` | 新增 6 项真实正文故障、取消及错误响应检查。 |
| `/Users/xxd/Playground/video-scrap/tests/test_current_page_and_controls.py` | 新增 9 项真实 Qt 导航/seek 与精确去重检查。 |
| `/Users/xxd/Playground/video-scrap/tests/http_fixtures.py` | 模拟后端失败时补上“已检查清单”的前置条件。 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_audit_oct18.py` | 旧本地文件夹具改走真实回环服务，继续检查缺片/短音轨与正常回退。 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_cancellation.py` | 后备取消夹具补齐清单检查前置条件。 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_name_reservation.py` | 文件发布失败夹具保留完整清单检查状态。 |
| `/Users/xxd/Playground/video-scrap/tests/test_hls_byte_ranges.py` | 检查固定本地清单内容和元数据，适应新入口地址。 |
| `/Users/xxd/Playground/video-scrap/tests/test_hls_integrity_audio.py` | 用原生解析器不可用触发后备，继续验证双轨要求。 |
| `/Users/xxd/Playground/video-scrap/tests/test_wrapped_hls_download.py` | 验证第二层快照仍提供同样解包字节，并确认未暗中改走其他后端。 |
| `/Users/xxd/Playground/video-scrap/audit_fixes_2026-10-22.md` | 本次记录。 |

既有测试断言没有改成“出现任意失败就通过”。模拟的后端故障必须建立正确的已检查输入状态；输出保护、缺片/短轨拒绝、正常后备和取消断言继续保留。

## 4. 最终验收与环境

最终完整回归：

```bash
cd /Users/xxd/Playground/video-scrap
PYTHONDONTWRITEBYTECODE=1 build/.venv/bin/python scripts/run_tests.py \
  --output build/final-fixes-2026-10-22/verified-regression --timeout 150
```

结果：57 个模块，611 项执行，0 失败、0 错误、0 跳过。新增 43 项相对于原 568 项；开发过程中的重复运行不额外累计。

```bash
PYTHONDONTWRITEBYTECODE=1 PATH='' \
  /Users/xxd/Playground/video-scrap/build/.venv/bin/python \
  /Users/xxd/Playground/video-scrap/main.py --smoke-test \
  /Users/xxd/Playground/video-scrap/build/final-fixes-2026-10-22/source-smoke-final.json
```

真实 Cocoa 源码集成共 15 项，覆盖真实帧、直链/HLS/包装视频、音频导出、全屏、音量、WebEngine、设置及独立暂停/恢复/取消。使用回环媒体、空 PATH 和临时设置；最终结果见下列汇总，旧安装包不包含本轮修改。

- 完整回归：`/Users/xxd/Playground/video-scrap/build/final-fixes-2026-10-22/verified-regression/summary.json`。
- 最终应用集成：`/Users/xxd/Playground/video-scrap/build/final-fixes-2026-10-22/source-smoke-final.json`。
- 冻结源码快照：`/Users/xxd/Playground/video-scrap/build/final-fixes-2026-10-22/source-manifest.json`，102 个源码/测试/配置文件。
- 最终一致性、时间与验收结果：`/Users/xxd/Playground/video-scrap/build/final-fixes-2026-10-22/verification.json`。

本机检测环境：macOS 27.0.1 arm64，Python 3.12.14；PyQt6 6.11.0 / Qt 6.11.2；requests 2.34.2、urllib3 2.8.0、yt-dlp 2026.8.19、psutil 7.2.2。运行解释器为 `/Users/xxd/Playground/video-scrap/build/.venv/bin/python`。

## 5. 剩余边界与交付状态

- 本轮验证的是 macOS 源码；Windows 原生、远程 CI 和新安装包仍需后续验收。
- 远程网站的登录有效期、服务端断流/限速和真实长期播放没有全部模拟；恢复可重试不等于服务端会无限保留连接或地址。
- 原有嵌套清单元数据的全局聚合上限观察、历史单次 HLS 502 的根因仍未在本轮升级为确定结论。
- 不把局部复现和通过测试解释为不存在其他 Bug；本轮已处理的是报告中的 9 项及验证中确认的相邻缺口。
- 没有新增应用二进制、Git 提交或推送。下次用户授权推送时，按项目规则为这一批修复更新一次版本。
