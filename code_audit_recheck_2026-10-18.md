# 当前代码第二轮独立复核

日期：2026-10-18，Asia/Kuala_Lumpur（UTC+08:00）。

**结论：这批修复有实质效果，旧问题不能继续原样列为未修复；但当前仍确认 8 项剩余或新发现的问题，其中 6 项 P2、2 项 P3，另有 1 项未实际触发的条件风险。现有 508 项回归与 15 项源码集成检查全部通过，并不能覆盖下面新增的边界。**

审查对象为当前工作区：HEAD 63194eafd892734259f2a069d76b042693ea090c，源码版本 0.3.3，加当前未提交修改。与上一份审查时的源码哈希作了对照；重新完整阅读变更模块和相关关键模块、核对新测试与旧测试变更，并独立重放旧输入和新边界。没有将 audit_fixes_2026-10-18.md 的修复声明直接当作证据。

范围为 35 个实现/工具 Python 文件、2 个打包配置、GitHub Actions 配置，以及 46 个测试模块和测试支持文件。实现/工具共 15,225 行。未变旧代码/测试结合前轮完整阅读和本轮哈希对照，不虚称所有旧测试本轮又逐行重读了一遍。第三方库、旧安装包和真实站点不在逐行审计范围内。

本次只创建报告和 build/audit-recheck-2026-10-18/ 下的复核材料，没有修改业务代码、既有测试、版本、旧报告，也没有提交、推送或重打包。

**一、当前确认的问题**

P2 为应安排修复的实质问题；P3 为影响较低或触发条件较窄的问题。复现中缩小时间/缓存预算是为了安全、快速地验证相同分支，不代表已经使用生产额度制造长时断流或写满磁盘。

**N01 · P2 · 合法超长媒体可使预览进程崩溃【真实媒体、真实播放器复现】**

位置：[preview_panel.py:877](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:877)，关联 [preview_panel.py:870](/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:870)。

QMediaPlayer 提供 64 位毫秒时长，代码直接传给只接受 32 位整数的 QSlider。超过 2,147,483,647 毫秒，约 24.86 天，就会在 setRange 抛 OverflowError。位置更新也直接传毫秒，存在相同边界。

本地生成 **1,452 字节、1 帧、30 天时长**的有效 MP4。真实播放器发出 durationChanged=2,592,000,000，应用现有槽函数抛异常；未安装异常钩子的独立进程退出 **-6 / SIGABRT**。这是实际 Qt 播放器信号触发，不是只手工传一个不可能的 Python 值。复现直接加载本地文件来隔离网络，没有宣称已在真实网站重放整条远程链路。

建议统一将滑条映射为秒、固定刻度或归一化进度，保留播放器原始 64 位毫秒值；同时处理时长、当前位置和用户 seek 三个方向。只截断最大值会丢失后段寻址。

证据：[GUI 详细报告](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/gui-review.md)、[实际进程输出](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/gui-long-duration-output.txt)。

**N02 · P2 · 包装 HLS 达到缓存预算后，后续片段持续返回 502【小预算复现】**

位置：[media_proxy.py:1435](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1435)，直接失败点 [media_proxy.py:1449](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1449)。

包装片段必须先完整解包并成功写入共享缓存，才能交给播放器。当前共享缓存额度为 512 MiB，但普通点播清单已播放过的子片段没有淘汰；达到额度后 write_at 返回 False，播放路径直接抛出“Playback cache budget reached”。清单仍持有旧缓存，重试不能释放额度，因此该视频后续片段持续失败。

用 **4,096 字节**的一块缓存预算重放：第一个包装子片段 GET 200；第二个 GET 502；重试仍 502，已用额度一直为 4,096。生产环境的对应触发条件是累计保留数据达到实际 512 MiB 额度，而不是本次真的下载了 512 MiB。

这是新增磁盘预算后的播放兼容性问题。旧 D13 的直播窗口淘汰有实质修复，但不能据此推导点播包装流在预算满时仍能播放。

建议缓存拒绝不能等同播放拒绝；可使用请求私有的受限解包暂存，或淘汰已消费且不在用的点播片段，并在全局额度、回看和在途请求之间定义清楚策略。

**N03 · P2 · 对整部影片应用 120 秒绝对读取期限，会截断健康的长响应【缩短期限复现】**

位置：[media_proxy.py:1873](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1873)、[media_proxy.py:1910](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1910)、[media_proxy.py:2027](/Users/xxd/Playground/video-scrap/src/media_proxy.py:2027)。

普通 MP4 的播放器请求可以是“从当前位置到文件尾”，但这些路径也套用分片式 READ_DEADLINE_SECONDS=120。即使持续有数据、网络没有空闲，超过绝对期限也中止上游。响应头已经发送完整 Content-Length，正文之后却短缺，异常没有在这里转换为透明的续传。

将期限缩为 **0.06 秒**，受控合法 206、稳定 ETag、64 字节正文每 0.02 秒产生 8 字节：代理已经宣布 200 / Content-Length 64，实际只发送 **16 字节**便抛出未处理 TimeoutError。模拟只压缩时间，不改变处理逻辑。

真实条件是大文件、低速连接或下游播放反压，使一次完整响应超过 120 秒。此证据证明响应被截断；没有据此声称所有 Qt 后端必然在播放第 120 秒终止，有些客户端可能重试。

建议把上游传输切成有限 Range 窗口并逐窗恢复，或使用适合持续播放的进展/空闲超时；小清单、密钥、有限片段可以继续采用绝对预算，不能把相同期限直接等价应用于整部影片。

**N04 · P2 · ETag 没有绑定最终资源 URI，原 D04 仍有重定向变体【下载/HLS/代理复现】**

位置：[parallel_downloader.py:239](/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:239)、[parallel_downloader.py:263](/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:263)、[downloader.py:1555](/Users/xxd/Playground/video-scrap/src/downloader.py:1555)、[media_proxy.py:497](/Users/xxd/Playground/video-scrap/src/media_proxy.py:497)。

当前发送 If-Match 并比较强 ETag，已经能拒绝同一资源的改变、丢失或弱验证器；但忽略 response.url，仍以重定向前 URL 保存身份。不同最终资源可以合法使用相同标签字符串，例如都用 "1"，它们不因此具有相同字节。这一点也核对了 [RFC 9110 §8.8.1](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.8.1) 对强验证器作用域的规定。

离线模拟原 URL 在 /object-a 与 /object-b 间重定向，两资源长度均 8,192、强 ETag 均为 "1"，但正文分别全 A、全 B。当前实际并行下载仍成功组合成 **2,048 A + 6,144 B**；当前 HLS 范围读取也得到相同组合；代理存在同根路径和独立缓存复现。

触发需要最终资源变动且标签碰巧相同，不能描述成“现在任何 ETag 校验都无效”。这也意味着上一份修复记录中“D04 已全部处理”的表述需要收窄。

建议最终规范资源 URI 与强验证器共同绑定，探测后固定资源，或者最终 URI 变化时清空重启/拒绝组合。不同 URI 的相同 ETag 不能直接充当跨资源内容哈希。

证据：[下载详细报告](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/download-review.md)、[实际复现输出](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/download-reproduction-output.txt)。

**N05 · P2 · 响应头看门狗取消存在竞态，可在返回成功后关闭套接字【确定性交错复现】**

位置：[browser_cookies.py:156](/Users/xxd/Playground/video-scrap/src/browser_cookies.py:156)、[browser_cookies.py:173](/Users/xxd/Playground/video-scrap/src/browser_cookies.py:173)。

getresponse 创建 Timer，回调捕获套接字后直接 shutdown；finally 中 timer.cancel 不能撤回已经越过 Timer 内部取消检查的回调。若此时线程被切走，主线程可以先成功返回响应，再由迟到的回调关闭正在读正文、甚至已被复用的套接字。

用与标准 Timer.run 相同的“检查 finished 后才调用回调”结构插入可控调度门，配合真实 socketpair 验证：getresponse 成功返回，套接字能读到 A；释放迟到回调后读到 EOF。没有连接外部服务，也没有改业务代码。

当前响应正文看门狗已有完成状态保护，但响应头 Timer 没有同样的生命周期互斥。此处需要头部完成接近期限或线程调度交错；不能将它直接断言为修复记录中那次偶发 502 的根因。

建议头部完成/取消和回调 shutdown 使用同一状态与互斥协议；在返回成功前建立“该 Timer 已无权关闭此响应”的保证。只有 cancel 调用不够。

证据：[复现脚本](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/header_timer_race.py)、[输出](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/header-timer-output.txt)。

**N06 · P2 · 原生 HLS 分片和排队 Future 数没有上限【真实队列复现】**

位置：[downloader.py:1302](/Users/xxd/Playground/video-scrap/src/downloader.py:1302)、[downloader.py:1393](/Users/xxd/Playground/video-scrap/src/downloader.py:1393)、[downloader.py:1033](/Users/xxd/Playground/video-scrap/src/downloader.py:1033)。

清单限制 2 MiB 不等于限制片段数。当前原生解析器默认/复用 pending_duration，不要求每个 URI 有独立 EXTINF；之后为全部片段建立路径，并一次性提交所有 Future。16 个工作线程只限制正在运行数量。

一个 **40,034 字节**清单、仅一个 EXTINF、后跟 20,000 个短 URI，完整经过当前获取/校验后得到 **20,000 个片段**。实际 ThreadPoolExecutor 中 16 个运行、**19,984 个排队**。传输使用门控 fake，随后取消、释放并确认监督线程退出，没有访问网络或制造 OOM。

即使合法长清单也会一次性排队；畸形短 URI 只是放大这个缺口。原 D09 的单响应字节/内存预算有实质修复，但不能保护本地模型和任务数量。

建议严格校验 EXTINF/URI 配对、限制清单资源数，并按有界窗口补充任务。代理清单解析已有资源数与结构保护，原生下载路径应保持一致契约。

**N07 · P3 · 清单回滚不恢复已有子资源类型，失败刷新可破坏后续播放【确定性复现】**

位置：[media_proxy.py:2080](/Users/xxd/Playground/video-scrap/src/media_proxy.py:2080)、[media_proxy.py:2261](/Users/xxd/Playground/video-scrap/src/media_proxy.py:2261)。

新事务回滚只删除本次新增的 children。遇到已存在资源时，_wrap_relative 会立即把 force_manifest/proxy_ext 改成清单类型；后续解析失败时，这些旧对象的修改没有回滚。

先加载有效媒体清单，把 clip.ts 注册为片段；失败刷新先用 STREAM-INF 把同 URI 当清单，随后非法 EXTINF 触发 ValueError。旧 clip.ts 仍保留 force_manifest=True、扩展 m3u8。再加载原有效媒体清单，属性也不恢复，实际 TS 后续会进入清单解析。

建议先纯解析完整计划再提交，或保存并回滚已有对象的全部可变角色属性。只撤销新增条目不足以构成完整事务。

**N08 · P3 · 源码检查的 FFmpeg 发现规则与应用不一致【无 PATH 条件复现】**

位置：[smoke_app.py:179](/Users/xxd/Playground/video-scrap/scripts/smoke_app.py:179)，关联 [tests.yml:32](/Users/xxd/Playground/video-scrap/.github/workflows/tests.yml:32)。

应用 resolve_ffmpeg 支持 build assets 和 imageio-ffmpeg；源码检查仍只调用 shutil.which，然后报“Bundled ffmpeg is missing from PATH”。干净源码环境中，实际应用能够找到 FFmpeg，检查却失败。

只模拟 PATH 中没有 ffmpeg：应用解析器成功返回现有资产路径，run_smoke_test 却退出 1、dependencies=false。这个失败发生在开始媒体检查前。

新工作流直接跑源码检查，但未显式将 fallback 工具加入 PATH。是否触发取决于 runner 是否另有系统 FFmpeg，不能据此宣称 GitHub Actions 必然失败；本轮也没有远程运行 CI。

建议源码检查使用同一解析器；frozen 模式若要断言“必须来自 bundle”，应另设严格来源检查，不能让源码模式误报。

证据：[失败报告](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/source-smoke-no-path.json)、[复现脚本](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/source_smoke_ffmpeg_repro.py)。

**二、仍需验证的条件风险**

POSIX 所有权对象只保存裸 PID/PGID，之后 [process_owner.py:88](/Users/xxd/Playground/video-scrap/src/process_owner.py:88) 直接 killpg。根进程在 [download_job.py:657](/Users/xxd/Playground/video-scrap/src/download_job.py:657) 被回收后，发布/回调到最终清理之间，若进程组已经消失且编号被复用，历史数字不再足以证明仍拥有该组。

这是需审查的 P3 条件性风险，需要号码复用和时序配合；本次没有耗尽 PID、终止无关进程，或观察到误杀。现有 stale-group 名称测试从未 attach，不能覆盖这一窗口。早期未枚举后代的握手/进程组修复本身已经有本机证据，不应回退到旧版“肯定留下孤儿”的结论。

建议将 POSIX 组的身份和回收时序一并设计，特别是根已 wait/reap 后的清理；Windows Job Object 不走此分支，仍需 Windows 原生验收。

**三、上轮 23 项的当前状态**

“原路径通过”只指相应原触发条件的保护获得代码和测试支持，不代表该功能已被形式化证明不存在任何其他问题。

| 原编号 | 当前复核结论 |
|---|---|
| D01 | 原普通 HLS 最终回退缺首/中/尾片成功发布已拒绝；真实正常回退仍成功。 |
| D02 | 原短音轨被容器时长掩盖已加入逐轨覆盖检查；短轨及偏移测试通过。 |
| D03 | 原空文件、明确 HTML/JSON/XML/图片错误响应已拒绝；不等于任意格式完整解码保证。 |
| D04 | **部分解决**：同一资源改变/丢失/弱 ETag 与无强验证器拼接已有保护；N04 的最终 URI 变化仍存在。 |
| D05 | 原跨协议、端口、自定义头重定向传播已按完整 origin 和每跳策略处理；Cookie 仍独立限域。 |
| D06 | 原属性注入重放后 anchor 保持原 HTTP URL，外链打开另有 HTTP(S) 门禁。 |
| D07 | 原快速失败紧循环已有可取消退避；对应计数/时序回归通过。 |
| D08 | 原直接媒体全量 HTML 扫描已跳过，普通 HTML 有字节和取消预算。 |
| D09 | 原 chunks+join 与无界单响应路径已改；但 N06 暴露分片/任务数量另一个边界。 |
| D10 | 缩略图改为工作线程 Qt 网络、有限传输和解码后缩放；真实网络/边界回归通过。 |
| D11 | 预取遇 200 不再读到整个文件 EOF；目标范围/大跳转测试通过。 |
| D12 | 预取已保存逻辑范围，并按区间请求和去重；共享对象测试通过。 |
| D13 | 直播旧子资源有宽限淘汰与额度；原窗口累积路径已处理。N02 是点播包装流预算满后的新问题。 |
| D14 | GUI 不再 wait/terminate 缩略图/抓取线程；20 次快速替换保持 6 个实际工作线程，资源正常释放。 |
| D15 | 相同真实 Gab 元数据输入现在把 inf 高度规范化为 None；选流正常。N01 来自真实播放器有限但超 32 位的时长。 |
| D16 | X 浏览器直链 Play 现已启用，会话参数统一；没有访问真实 X 登录站点。 |
| D17 | 原迟到 ready/failed 被代次和控件存活检查忽略。 |
| D18 | finished 后回收，独立生命周期探针最终后台注册数为 0。 |
| D19 | Next 嵌套类型防御和独立 TanStack 回退已补；旧畸形输入恢复有效结果。 |
| D20 | 锚点按最新播放/绝对序列更新，回退/直播重映射已有测试。 |
| D21 | HTML、只有标记的错误文字、悬空结构不再当成功清单；N07 是失败回滚对旧对象的另一缺口。 |
| D22 | 保存、内存提交、通知顺序已序列化；回调再入与取消订阅测试通过。 |
| D23 | 原占位+replace 删除，采用原子不覆盖发布或明确失败；当前 macOS 抢占测试通过。 |

原 R01–R12 也有对应改动：有界预取池、主动取消、所有者标记、进程归属握手、状态有序派发、可移植文件名、解码器预算、JS 代次、日志遮蔽、取消订阅、会话契约和 WebEngine availability。当前仍应保留 N03/N05 的取消/期限边界、上面的 PGID 风险，以及 Windows 原生、总路径和未知自定义路径秘密的验证限制。

工程改进已经新增统一隔离入口、macOS/Windows 工作流、发布依赖快照、Mac Git 容错和 Windows VERSIONINFO，删除了无效 GUI lane 调度。它们不代表 CI 已远程运行、Windows 已验收或新安装包已生成；N08 表明新的验收入口自身还需要收紧。

**四、本轮实际验证**

- [完整回归汇总](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/tests/summary.json)：**46 个文件、508 项执行、0 失败、0 错误、0 跳过**。
- [源码应用集成](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/source-smoke.json)：**15 项通过**，macOS Cocoa、frozen=false、版本 0.3.3；运行时显式提供已有 FFmpeg 路径，不能用此推翻 N08 的干净 PATH 复现。
- 全部项目 Python/spec 完成 AST 解析，git diff --check 通过。
- 新复现仅使用本地真实媒体、socketpair 或受控响应；没有真实站点、登录 Cookie、OOM/磁盘写满试验或对无关进程发信号。
- 独立新探针不重复计入 508+15，也不会因为“成功证明缺陷”的脚本退出 0 就将其计为产品通过。
- 测试环境为当前 macOS arm64、Python 3.12.14、PyQt6 6.11.0。本轮未验证 Windows 本机、GitHub 远程 CI、新打包二进制和真实长时间网络播放。

**五、证据与建议顺序**

下载：[详细复核](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/download-review.md)、[复现脚本](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/reproduce_download_recheck.py)。

代理/抓取：[详细复核](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/scraping-proxy-review.md)。缓存预算、整响应期限、回滚及代理资源身份的脚本/原始输出均在同一目录。

GUI：[详细复核](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/gui-review.md)。该目录的超长时长复现默认安装证据钩子；无保护模式只用于有超时和父进程监管的隔离进程，不应直接在日常应用中重放。

总体源码快照：[source-manifest.json](/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-18/source-manifest.json)。

建议先处理 N01 的崩溃、N02/N03 的正常播放兼容性及 N04 的数据一致性；同时修 N05 生命周期竞态与 N06 任务数量边界。随后处理 N07 回滚完整性和 N08 验证入口，再补 Windows 和重新打包验收。本次仅审查，没有实施这些修复。
