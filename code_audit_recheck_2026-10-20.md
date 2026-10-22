# 当前代码再次复核：2026-10-20

日期：2026-10-20，Asia/Kuala_Lumpur（UTC+08:00）。

## 1. 本次结论

**上轮 R01–R09 共 9 项问题，本轮均重新触发；有限追加检查没有确认新的问题。** 当前仍为 4 项 P2、5 项 P3。业务源码没有变化，不能将此前的提交理解为这些新边界已经得到修复。

审查基点：`b8cf63298e592afcda8e88520e490610942937dc`，源码版本 `0.3.4`。与 2026-10-19 保存快照比较，95 个源代码/测试/配置文件全部一致。本轮仅新建记录及 build 下的本地验证资料，没有修改业务代码、既有测试、版本或旧报告，没有提交、推送、重打包。

本轮重点是重新运行先前失败的边界和有限相邻代码复核；没有重新宣称逐行审计整个代码库。完整根因和建议仍见 `/Users/xxd/Playground/video-scrap/code_audit_current_2026-10-19.md`。

## 2. 今日实际复现结果

| 编号 | 等级 | 问题及今日结果 | 主要代码位置 |
|---|---|---|---|
| R01 | P2 | HLS 原生请求 503 失败后，清单变为合法字节范围，上游对两次 Range 错误返回整对象；真实后备下载仍将预期 2 秒发布为 4 秒。 | `/Users/xxd/Playground/video-scrap/src/downloader.py:618`、`:2069`、`:1882` |
| R02 | P2 | 暂停时间被算入 HLS 总读取期限。真实进程暂停约 0.5035 秒，恢复后超时失败；不暂停对照完成。 | `/Users/xxd/Playground/video-scrap/src/downloader.py:1591`、`/Users/xxd/Playground/video-scrap/src/download_job.py:648`、`:659` |
| R03 | P2 | JS 转换后缺少长度预算。366 字符输入经 10 次替换返回 1,741,945 字符候选，超过 65,536 限制。 | `/Users/xxd/Playground/video-scrap/src/js_decoder.py:156`、`:170`、`:279` |
| R04 | P2 | 浏览器从旧页导航到新页仍累计两页素材；主窗口给旧素材使用新页 Referer，实际 403，正确旧页来源对照 200。 | `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:295`、`:304`、`:326`、`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:945` |
| R05 | P3 | 普通 HLS 清单正文截断或坏 gzip，Requests 异常逃出处理器，直接断连接且应用状态日志为空。正常对照返回 200。 | `/Users/xxd/Playground/video-scrap/src/media_proxy.py:1656`、`:1687` |
| R06 | P3 | 键盘 End 只把滑条变成 60000，真实 QMediaPlayer 仍在 20000 毫秒，没有 seek。 | `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:227` |
| R07 | P3 | AES-CBC 无效零填充仍通过解密、FFmpeg 合并和发布。完整解码正常，仅证实输入校验缺口。 | `/Users/xxd/Playground/video-scrap/src/downloader.py:1667`、`:2361` |
| R08 | P3 | 一个无关 5000 位整数声明抛 ValueError，导致同页其他正常隐藏源遗漏；普通 video 仍保留。 | `/Users/xxd/Playground/video-scrap/src/js_decoder.py:71`、`:98`、`/Users/xxd/Playground/video-scrap/src/scraper.py:612` |
| R09 | P3 | 真实本机 yt-dlp 与 HTML 扫描同一单视频页面，仍返回网页条目和同一 MP4 direct 条目两条结果。 | `/Users/xxd/Playground/video-scrap/src/scraper.py:539`、`:561` |

### 重要复现边界

- R01 使用两个真实、独立 1 秒 HLS TS，长度 40,984 / 37,412 字节，均按 188 字节对齐。后备下载、503、Range 错误响应、yt-dlp、FFmpeg、发布全部真实执行。需要原生先失败、清单变化、服务器忽略 Range 的组合条件，不是普通下载无条件损坏。
- R02 只将生产 120 秒预算缩为 0.3 秒，使用真实 DownloadJob、spawn、所有者进程和 OS 暂停/恢复；没有实等 120 秒。证据支持暂停计时缺口，不推断所有下载阶段都会失败。
- R03 没有制造 OOM；确认的是转换后的候选突破预算，持续扩大可能占用大量内存与 CPU，未声称已观察到应用崩溃。
- R04 使用真实本机 Chromium 页面、真实主窗口导入和 HTTP 403/200 对照；没有用户 Cookie 或外部登录站点。
- R05 的上游正文已经不可用，完善异常处理主要改善诊断和响应一致性；没有全局应用崩溃或坏缓存证据，保持 P3。
- R06 通过离屏 Qt 的真实 QTest 和 QMediaPlayer 复现 End；正常鼠标拖动及音量不是本项缺陷。
- R07 只有 HTTP 输入使用离线 fake，AES/FFmpeg/发布真实执行；产物完整解码退出 0、无错误输出，不能据此声称画面受损，也不能将 padding 校验当认证。
- R08 的实际 scrape 对照仍能发现 normal.mp4，只遗漏 hidden.m3u8，不是整页抓取崩溃。

## 3. 有限追加检查

### 本轮未找到新反例的范围

- 下载侧重读 POSIX anchor、父进程 EOF、显式终止、清理及无验证器整对象 spool；未确认额外缺陷。旧监督器向历史 PGID 发组信号的路径当前不存在。
- 地址身份小探针：规范协议/主机/默认端口及 fragment 别名保留已有 `ABCD` 缓存；相同 ETag 但不同查询 URI 被拒绝，缓存内容和预算归零。
- 嵌套清单小探针：深度 16 可注册，第 17 层被拒绝，失败没有残留新增条目。
- JS renderer 的即时页面跳转对照仍正常返回一个视频，未复现可疑的 loadFinished 提前中断。
- 本机单实例的 12 项检查通过。
- HTTP 响应归属代次及包装正文槽做有限源码复核，未发现新反例；没有把昨日竞态回归计数当成今日重新执行。

### 仍需保留的条件观察

3 个变体、每个 3 个片段的小清单创建 13 个空条目/临时文件；正文缓存用量仍为 0，说明字节配额不包含条目和文件元数据。已有单清单数量与深度限制，但缺少较小的跨层聚合数量上限。本轮没有耗尽内存、磁盘或制造可见故障，因此不计入第 10 项已确认 Bug。

Windows 原生、新安装包、GitHub 远程 CI、真实 TLS/外部代理及真实站点长时间运行没有在本轮验收。历史单次 HLS 502 仍未确定根因，不把 R05 自动等同于那一次错误。

## 4. 新证据位置

所有今日材料位于 `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/`，昨日材料保留。

| 内容 | 完整路径 |
|---|---|
| 开始源码快照 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/source-manifest.json` |
| JS 长度与整数重放 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/root-results.json` |
| 合法 TS 字节范围重复拼接 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/downloads/valid-range-result.json` |
| 暂停/不暂停对照 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/downloads/paused-hls-result.json` |
| AES 填充结果 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/downloads/aes-padding-result.json` |
| 两个产物完整解码确认 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/downloads/decode-confirmation.json` |
| 跨页来源、403/200 与键盘 seek | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/gui/reproduction.json` |
| 整数声明导致隐藏源遗漏 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/gui/scraper-decoder-boundary.json` |
| 同一媒体重复列出 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/gui/ytdlp-html-overlap.json` |
| 清单传输异常、堆栈、正常对照 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/proxy/manifest-transport-result.json` |
| 身份与嵌套限制小探针 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/proxy/small-boundaries-result.json` |
| 最终首尾哈希及结果核验 | `/Users/xxd/Playground/video-scrap/build/audit-recheck-2026-10-20/verification.json` |

## 5. 验证口径与建议

- 本轮没有重复运行完整 568 项回归或 15 项 Cocoa 源码集成；它们是 2026-10-19 的历史通过结果。源码未变化，此次直接重放未被那些测试覆盖的失败边界。
- 项目 Python/spec 语法再次通过 AST 解析；受检源码/测试/配置首尾哈希无变化。
- 复现探针正常退出只表示已记录产品缺口，不等于产品通过。子进程及本机服务器仅属于测试自身，均由探针结束清理。
- 建议先处理 R01 的错误成品发布、R02 的暂停计时，再处理 R03 的转换预算和 R04 的素材来源；R05–R09 随后修复并补针对性回归。
- 当前结论支持“9 项仍需修复”；没有证据支持宣称整个代码库再无其他问题。
