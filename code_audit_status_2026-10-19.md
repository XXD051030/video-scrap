# 最新剩余问题与复核状态

日期：2026-10-19，Asia/Kuala_Lumpur（UTC+08:00）。

**当前结论：上轮 N01–N08 的原触发路径均有有效修复并通过本轮复测；本次另确认 1 项 P3 缓存边界，保留 1 项未实际触发的 POSIX 进程组风险。Windows、远程 CI、新安装包和真实远程长时间运行仍未验收。**

对象为 HEAD 63194eafd892734259f2a069d76b042693ea090c、版本 0.3.3 加当前未提交修改。与上一轮保存哈希比较，10 个既有受检文件改变、5 个新增测试模块。本轮聚焦这些改动和上轮 8 项，不是重新宣称对全部未变化代码进行了完整审计。

没有修改业务代码、测试、版本或历史报告，没有提交、推送、重打包；只新增本报告及 build/audit-status-2026-10-19/ 下的验证材料。

**一、仍需修复的已确认问题**

**C01 · P3 · 包装资源成功切换类型后，旧片段缓存仍会被复用**

位置：[media_proxy.py:2334](/Users/xxd/Playground/video-scrap/src/media_proxy.py:2334)、[media_proxy.py:1493](/Users/xxd/Playground/video-scrap/src/media_proxy.py:1493)。

触发需要同一上游 URI 在当前会话中先作为媒体片段、再成功作为清单、之后又回到媒体片段，且内容已经变化。普通使用固定资源 URI、类型及内容不变的点播不触发。

当前成功提交新角色时只更新 force_manifest、proxy_ext 和索引，没有重置这个对象的旧完整缓存、total_size 等表示状态。重新回到片段角色后，_ensure_wrapped_cache 看到旧缓存已经完整便直接返回，跳过重新获取当前内容。

本地无网络复现：

1. 有效媒体清单注册 clip.png；片段正文 OLD 完整进入缓存，GET 200。
2. 有效主清单把同 URI 作为子清单；实际处理一次新的有效清单正文，GET 200，旧缓存仍为 OLD，total_size 仍为 3。
3. 有效媒体清单再次将其作为片段，模拟源端当前正文为 NEW；GET 仍返回 OLD，新的正文获取次数为 0。

此处使用 OLD/NEW 合成正文检查缓存路由，未声称使用真实站点播放了完整影片。复现中三个清单均合法、角色变更均成功，没有走失败回滚路径。因此上轮 N07 的“失败刷新污染角色”确实已修复，C01 是成功变更角色时的缓存失效缺口。

建议角色改变时重建对应资源条目，或在提交后安全失效旧表示的缓存、长度和其他相关状态；同时保护仍在处理旧角色的请求。只更新角色标签不足以保证下一次读取使用当前内容。

证据：[复现脚本](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/wrapped_role_cache_repro.py)、[实际输出](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/wrapped-role-cache-output.txt)。

**二、仍保留的条件性风险**

**POSIX 裸 PGID 复用窗口：未解决，尚未观察实际误杀。**

[process_owner.py:80](/Users/xxd/Playground/video-scrap/src/process_owner.py:80) 保存数值组号，[process_owner.py:88](/Users/xxd/Playground/video-scrap/src/process_owner.py:88) 用该号码执行 killpg；文件与上一轮相同。

原组已经为空且根进程已回收后，如果号码被新进程组复用，再延迟执行旧任务清理，裸数字不再足以证明归属。此风险需要特殊复用和调度条件，本轮没有制造号码耗尽、向无关进程发信号或观察到误杀。

初始握手/私有组已保护正常的早期后代归属，不能将这个窄窗口泛化成普通取消必然不安全。后续应审查组身份和根进程回收的整个生命周期；Windows Job Object 不走这个 POSIX 分支。

**三、上轮 8 项当前状态**

| 上轮编号 | 本轮核对结果 |
|---|---|
| N01 超长时长崩溃 | 原真实 30 天 MP4、原无异常钩子脚本，当前正常退出 0（此前 -6）；位置/时长/seek 均做双向整数映射。新增 4 项回归通过。 |
| N02 包装流缓存满后持续 502 | 配额不足时使用受限请求私有正文，不把缓存拒绝当播放拒绝；并发正文生命周期有两个槽。缓存满时 GET/HEAD/Range/重试、慢输出和预取引用回收测试通过。 |
| N03 整文件 120 秒截断 | 根媒体改为进展/空闲超时，并排除下游消费时间；有限 HLS 资源仍保留绝对预算。持续小包、慢消费、阻塞取消及字节限额测试通过。 |
| N04 最终资源不同但 ETag 相同 | 下载、HLS 和代理将规范最终 URI 与验证器/长度一起核对；变化拒绝组合。同目标 alias、稳定目标及实际本地重定向测试通过。 |
| N05 头部 Timer 取消竞态 | 完成/超时/shutdown 共享生命周期锁。原确定性交错脚本现在返回成功后仍能读到 B（此前 EOF）；正文增加连接代次保护。 |
| N06 HLS 巨量排队 | 独立有限正数 EXTINF、资源数预算及先验证后读取；未完成 Future 窗口不超过 2×workers。长清单取消和两轨交错测试通过。 |
| N07 失败清单回滚不完整 | 既有角色和完整计划有快照与恢复；失败及提交阶段异常测试通过。另发现上面 C01 的成功角色变化缓存问题，不能混为同一个旧触发路径。 |
| N08 源码检查只找 PATH FFmpeg | 源码自检共用应用解析器，frozen 模式另检查 bundle 来源。9 项新规则回归通过；本轮真实清空 PATH 的 Cocoa 集成全部通过。 |

N01 的独立重放和 N04/N06 详细核对见 [GUI 当前复核](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/gui-review.md)、[下载当前复核](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/download-review.md)。本轮对代理/HTTP 改动重新核对关键实现和对应新测试，C01 由主审查进程完成独立复现。

**四、仍需验收或观察的事项**

- Windows 原生的 Job Object、文件系统、播放与取消；GitHub Actions 远程实际运行。
- 重新生成安装包并做 frozen 检查。当前旧 App/ZIP 不会自动包含工作区修复。
- 真实 TLS、外部代理、真实远程长时间播放；本地及缩短预算检查不能代替这些场景。
- 历史单次 HLS 502 的根因仍未确定。本轮集成没有出现该错误，但不能反推历史原因已被证明。
- 任意无标识路径秘密的日志识别、任意文件系统总路径长度等原已明示的能力边界仍存在，没有升级成新的已复现故障。

**低优先级性能观察：** [downloader.py:1387](/Users/xxd/Playground/video-scrap/src/downloader.py:1387) 的主清单选流反复复制剩余列表。下载复核中 1,000/2,000/4,000/8,000 变体约耗时 0.0046/0.0118/0.0376/0.1328 秒。只支持超线性趋势，没有证明普通站点明显卡顿或 OOM；可改为单次扫描，并限制 master 变体数。本轮没有扩大压力级别。

**五、本轮实际验证**

- [完整回归](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/tests/summary.json)：**51 个模块、559 项执行，0 失败、0 错误、0 跳过**。
- [源码应用集成](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/source-smoke.json)：**15 项通过**，真实 Cocoa、PATH 为空、版本 0.3.3、frozen=false。
- 原头部 Timer 脚本重放：[输出](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/header-timer-replay.txt)。
- 受检 Python/spec AST 解析和 git diff --check 通过；本轮源码哈希未改变。
- [源文件快照](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/source-manifest.json)、[验收汇总](/Users/xxd/Playground/video-scrap/build/audit-status-2026-10-19/verification.json)。

以上通过不包含 C01 的新失败验收条件；其复现脚本成功退出表示成功证明了缺口，而非产品行为通过。当前没有依据把旧 N01–N08 再原样当成 8 项未修复问题，也没有依据宣称整个代码库不存在其他问题。
