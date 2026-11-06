# 剩余缓存与进程归属问题修复

日期：2026-10-19，Asia/Kuala_Lumpur（UTC+08:00）。

本轮按用户“接着修”的要求，处理 code_audit_status_2026-10-19.md 中的已复现缓存问题 C01、POSIX 裸 PGID 条件风险，并顺手移除主清单选流的重复列表扫描。修改保留在当前工作区，版本仍为 0.3.3；没有提交、推送、生成安装包或改写历史报告。

**结果：上述两项的具体原因已处理；52 个模块、568 项去重回归和 15 项真实 Cocoa 源码集成检查通过。此结论不等于保证整个项目没有其他缺陷，也不代替 Windows 或新安装包验收。**

**一、C01：资源角色切换后读取旧缓存**

原问题是同一 URI 先作为媒体片段、再作为清单、之后又回到片段时，只更新类型，仍保留原来的正文、长度和缓存身份。

现在角色发生变化时创建新的 Entry、播放 token 和缓存。当前清单验证与提交成功后，旧条目才标记为 superseded；失败解析或提交仍恢复原查找表、原缓存和播放计划。同角色正常刷新继续复用条目和缓存，不会每次都重新下载。

已开始的旧 HTTP 请求可以继续读旧正文。旧条目或其后代仍有活动请求时不关闭；最后一个请求结束后回收旧树、缓存文件、token 和预算。这样新的类型不会误读旧字节，也不用在角色提交时等待慢播放器完成。

主要实现：[media_proxy.py](/Users/xxd/Playground/video-scrap/src/media_proxy.py)。

新增 [test_proxy_role_cache.py](/Users/xxd/Playground/video-scrap/tests/test_proxy_role_cache.py) 的 4 项有行为意义的回归：

- 合法“片段→清单→片段”后读取 NEW，不再返回 OLD；旧缓存和嵌套计划被回收。
- 类型不变时保留缓存和 token，不发起额外获取。
- 解析失败、提交阶段故障注入均保留原条目、正文与缓存预算。
- 旧清单下正在输出的后代请求继续完成 OLD，同时新条目成功提供 NEW；请求结束后旧树删除。

原成功角色切换测试更新为检查新旧条目隔离；原失败回滚保护继续保留。缓存相关 73 项定向检查通过，随后纳入完整回归。

**二、POSIX：不再向历史 PGID 发送信号**

原来的监督器在根进程已退出/被回收后，仍保存数值 PGID 并调用 killpg；该号码在特殊时序下可能被复用。仅延迟某一个 join 不够，因为其他 multiprocessing 操作也可能自动回收子进程。

现在每个 POSIX 下载任务先创建一个持续存活的私有组长进程。下载工作者在执行后端前加入该组并完成所有权握手；监督器通过私有管道请求清理，**只有仍然存活的组长对自身当前进程组调用 killpg**。即使下载根进程已经被回收、另一个 multiprocessing 任务又启动，也不再需要监督器向一个历史号码发送组信号。

监督器意外退出导致管道 EOF，或正常解释器退出终止守护组长时，也走当前组的清理。控制进程意外消失时拒绝历史组信号；已知进程仍通过原 identity-aware psutil 句柄尽力清理，无法确认归属/退出就保留暂存目录并报告 DownloadCleanupError，不伪装成清理成功。

主要实现：[process_owner.py](/Users/xxd/Playground/video-scrap/src/process_owner.py)、[download_job.py](/Users/xxd/Playground/video-scrap/src/download_job.py)。

7 项原生所有权检查和 21 项既有 DownloadJob 检查通过，覆盖：

- 根进程先退出、尚未枚举的后代仍能清理。
- 根被回收、其他 multiprocessing 任务启动后，组长仍存活并保持组身份。
- 监督器进程中的 killpg 被禁止时，仍能通过组长清理；无关测试进程保持存活。
- 组长意外消失时拒绝历史信号、保留暂存和明确失败。
- 无效握手、构造前取消、重复关闭不留下控制进程。
- 监督器直接 os._exit 后，管道 EOF 仍清理后代。
- 真实下载、音频/HLS、暂停/恢复、并发任务与取消维持既有行为。

新增代价是每个活动 POSIX 下载任务多一个小型控制进程。Windows 沿用 Job Object，不创建这个 POSIX 组长；本轮没有 Windows 原生验收。组长退出后的数字组号只用于保守的退出检查，不再作为监督器的信号目标。

**三、主清单选流的重复扫描**

[downloader.py](/Users/xxd/Playground/video-scrap/src/downloader.py) 改为逆序单次扫描查找后续 URI，恢复原候选顺序以保留并列清晰度/带宽的选择行为。只为最终选中的变体构造绝对 URL，避免对重复的大 URI 反复创建字符串。

这项调整保持选流语义，没有新建只镜像实现的单元测试；现有质量、正常/异常范围与重定向检查重跑 45 项通过。当前环境的 1,000–8,000 变体测量见 [扫描结果](/Users/xxd/Playground/video-scrap/build/remaining-fixes-2026-10-19/master-scan-results.json)，不作为任何机器上的固定性能保证。

**四、最终验证及文件范围**

- 完整隔离运行：52 个模块、568 项执行，0 失败、0 错误、0 跳过。
- 最后一次选流字符串分配优化后，重跑相关 3 个模块的 45 项；将结果按模块替换，不重复累计成 613 项。
- 真实源码集成：清空 PATH、macOS Cocoa、15 项通过，含实际播放、HLS、音频转换、WebEngine、下载暂停/恢复/取消与暂存清理。
- Python/spec AST 解析、git diff --check 通过；最终源码与保存的验收清单匹配。

证据：

- [最终去重回归汇总](/Users/xxd/Playground/video-scrap/build/remaining-fixes-2026-10-19/final-regression-summary.json)
- [实际应用集成](/Users/xxd/Playground/video-scrap/build/remaining-fixes-2026-10-19/source-smoke.json)
- [验收摘要](/Users/xxd/Playground/video-scrap/build/remaining-fixes-2026-10-19/verification.json)
- [最终源码哈希](/Users/xxd/Playground/video-scrap/build/remaining-fixes-2026-10-19/source-manifest.json)

相对于本轮开始的快照，仅改动以下 7 个源码/测试文件，另新增本记录；已有大量未提交改动均保留：

| 文件 | 本轮作用 |
|---|---|
| src/media_proxy.py | 按角色隔离条目，提交后退休旧条目，保护在途后代请求 |
| src/process_owner.py | 私有持续组长、管道归属及安全失败 |
| src/download_job.py | 工作进程入组握手、控制进程标记、失败收尾 |
| src/downloader.py | 主清单扫描与 URI 分配优化 |
| tests/test_process_ownership.py | 扩展到 7 项原生归属/故障检查 |
| tests/test_proxy_recheck_oct18.py | 更新成功角色切换的条目隔离断言 |
| tests/test_proxy_role_cache.py | 新增 4 项缓存与活动请求生命周期回归 |

**仍未覆盖的验收：** Windows 原生、GitHub 远程 CI、新安装包、真实 TLS/外部代理和远程长时运行。历史单次 HLS 502 的根因仍无充分证据归因。本轮没有将它们标成“已通过”或新发现的已确认 Bug。
