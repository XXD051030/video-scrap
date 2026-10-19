# Video Scraper 0.3.1 代码审查报告

## 1. 基准、范围与结论

- 记录时间：2026-10-17 00:35（Asia/Kuala_Lumpur，UTC+08:00）；这是本轮取证时间，不是各历史问题首次发生时间。
- 项目：`/Users/xxd/Playground/video-scrap`。
- 审查提交：`e330f5e92375acd43fc8b4301eaa4b0aa6a6ef49`，分支 `main`，应用版本 `0.3.1`。
- 开始审查时，Git 跟踪的文件无工作区修改；已有未跟踪的 `assets/`、三份日记予以保留。
- 范围：67 个受版本控制文件，其中生产与打包代码 31 个文件、12,534 行，测试代码 29 个文件、9,081 行；另检查依赖、文档、忽略规则和项目约定。生产和测试代码已分模块阅读全文并核对跨模块调用。
- 静态统计找到 303 个 `test_` 函数定义；这不是本轮测试通过数量，也不代表 303 种现实环境已验证。
- 方法：合并读取文件与行号、Git 状态/日志、AST 清单、本地 Mock、离屏 Qt、合成媒体与真实 FFmpeg 的少量定向诊断。没有运行全套测试，没有实测 Windows 本机或重新验证打包二进制，没有访问真实视频站点或使用用户登录凭证。
- 本轮只新增本报告；应用源码、版本、设置和构建产物没有修改，也没有提交或推送。

**最先应处理的三项：普通 HLS 空片段导致截短文件仍报完成、分离音轨的 HLS 下载漏音轨、HLS 预览提前打开全部片段文件导致句柄耗尽。三项均已用受控输入复现。**

其余结果分为 B（具体缺陷或当前功能缺口）、R（有触发条件的风险）和 M（维护改进）。高优先级表示应先修，并不表示每次使用都会触发。本报告不能证明不存在未发现的问题；第三方源码、生成的构建目录、全部网站和全部操作系统组合不在逐行审查范围内。

## 2. 结构与主要数据流

```text
main.py
  ├─ SingleInstance：进程锁、重复启动唤起
  └─ MainWindow
      ├─ ScrapeWorker → VideoScraper → yt-dlp / HTML / JS 数据解析
      ├─ JSPageScraper / InteractiveScrapeDialog → 浏览器提取 → VideoItem
      ├─ VideoListWidget → ThumbnailWorker
      ├─ PreviewPanel → QMediaPlayer → MediaProxyServer → 上游媒体 + 临时缓存
      └─ DownloadWorker → DownloadJob 监督器 → spawn 下载子进程
          └─ VideoDownloader → 并行直链 / 原生 HLS / yt-dlp / FFmpeg
              └─ 私有 staging → 唯一名称发布 → 清理
SettingsStore → 主题、缓冲设置与代理订阅
build_macos.py / build_windows.py → PyInstaller spec → .app / Windows 文件夹与 ZIP
```

结构上最值得注意的边界是：抓取的任务身份与会话不能只靠一个共享 URL；HLS、DASH、完整文件与分片需要不同模型；正常下载返回成功必须同时保证内容完整和实际落盘；缓冲秒数、累计磁盘、打开文件数与排队请求数是四种不同额度。

## 3. 具体缺陷与功能缺口

### B01 · 高 · HLS 空中间片段导致截短文件仍显示完成【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:1324`、`:1350`、`:786`、`:1428`、`:929`。
- **触发**：三个各 1 秒的普通 TS 片段，中间片段 HTTP 200 但正文为空。
- **根因与影响**：非 Range 片段未拒绝零字节；合并只看 FFmpeg 退出码。实际 FFmpeg 在空中段处结束仍返回 0，第三段也没有进入成品，应用却发出 `completed`。
- **证据**：真实 FFmpeg、合成媒体和受控响应得到 24,229 字节 MP4，时长 1.00 秒；清单预期 3 秒，最终状态 `completed`。证实范围是空片段；其他损坏类型尚未逐一验证。
- **修复方向**：拒绝并重试空片段，补充普通片段有效性检查；合并后确认预期片段/时长和错误情况，不只依赖退出码。保留现有 Range 保护。

### B02 · 高 · 分离音视频的 HLS 正常下载漏掉独立音轨【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:746`、`:752`、`:980`、`:547`。
- **触发**：master 的 `EXT-X-MEDIA:TYPE=AUDIO` 指向独立音轨，选中视频子清单本身不含声音。
- **根因与影响**：只下载选中的视频列表；`master_context` 保留音轨的处理仅在原生失败后的 fallback 使用。原生成功直接返回无声 MP4。
- **证据**：实际请求仅有 `master.m3u8`、`high.m3u8`、`video.ts`，没有请求 `audio.m3u8`；真实输出有 Video、无 Audio。
- **测试缺口**：`/Users/xxd/Playground/video-scrap/tests/test_hls_byte_ranges.py:430` 主动使原生失败，验证 fallback；audio-only 用例不能证明正常视频包含音轨。
- **修复方向**：正常成功路径处理关联音轨并合并，或在识别独立音轨后使用已保证音视频完整的后端；不能绕开已有范围安全检查。

### B03 · 高 · HLS 清单为所有资源提前打开文件，可能耗尽句柄【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:323`、`:327`、`:1807`、`:1813`、`:1867`。
- **触发**：清单资源数超过进程剩余文件句柄额度。缓冲设为 0、片段尚未下载也会触发。
- **根因与影响**：每次注册 `_Entry` 就创建并持续打开一个缓存文件；重写清单立即注册全部片段。长片单次加载即可失败，并可能影响同进程后续网络/文件操作。
- **证据**：只对独立诊断进程设置软额度 128，解析 200 片段合成清单，在已打开 124 个空子缓存、媒体缓存为 0 字节时抛 `OSError: [Errno 24] Too many open files`。124 不是当前应用固定上限。
- **修复方向**：注册元数据与创建文件分开，首次写入才创建；限制同时打开文件数并关闭闲置句柄。此问题和累计磁盘额度 R02 需要分别处理。

### B04 · 中 · 点击进度条轨道后，拖动和自动进度更新可能失效【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:43`、`:59`、`:781`。
- **触发**：点击滑条轨道而非现有滑块，然后拖动、松开。
- **根因与影响**：手动 `setSliderDown(True)` 后提前返回，没有建立原生拖动状态或实现对应移动/释放。播放位置更新又在 `isSliderDown()` 为真时跳过。
- **证据**：真实离屏 Qt，400px 滑条点击 x=260 后 value=66；移动到 x=320、松手后仍 value=66、slider_down=True。音量使用相同控件，也受轨道拖动问题影响。
- **修复方向**：完整处理按下、移动、释放，保证松手清除状态；补真实鼠标事件检查。已有 `setValue()` 检查覆盖不到此路径。

### B05 · 中 · 普通 HLS 无扩展名子清单被当成片段【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1151`、`:1804`、`:1813`。
- **触发**：非 PNG 模式的 master 使用 `URI="audio-track"` 或 `video-track` 这样的合法子清单 URL。
- **根因与影响**：根据 master 标签强制标记子清单的逻辑只在 PNG 模式启用；普通 HLS 依赖 `.m3u8` 后缀。子清单被普通转发，内部相对分片地址没有改写。
- **证据**：audio-track/video-track 子项均得到 force_manifest=False、proxy_ext=ts、likely_manifest=False。
- **修复方向**：对普通和包装 HLS 都按标签识别子清单，必要时辅助检查响应内容。现有顶层无扩展名/PNG 子清单测试不覆盖该组合。

### B06 · 中 · 直链 DASH/MPD 被误分类为 HLS，下载路由确定失败【静态确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:104`、`:940`、`:969`；`/Users/xxd/Playground/video-scrap/src/downloader.py:531`、`:964`、`:1102`、`:536`；`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1155`、`:1271`。
- **触发**：HTML/浏览器发现的 `.mpd` 被构造成 `is_direct=True, is_hls=True`。
- **根因与影响**：XML MPD 进入 M3U8 验证，抛出不允许 fallback 的 `_HlsByteRangeError`，下载失败。预览虽启用播放，但代理不重写 MPD 的相对 BaseURL/分片链接，含相对地址时也会失败。
- **边界**：不能扩大为“yt-dlp 所有 DASH 下载都失败”；这里专指直接 MPD 项。绝对分段地址的特定预览也可能表现不同。
- **修复方向**：模型明确区分 HLS/DASH，使用对应下载路径；预览实现 DASH 代理或明确提示不支持。

### B07 · 中 · HLS 清晰度选项实际未生效【已复现选择路径】

- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:531`、`:948`、`:1126`、`:1518`。
- **触发**：master 提供多个清晰度，用户选 480p、720p 或 1080p。
- **根因与影响**：HLS 路由只区分 audio-only，其余未传目标清晰度；选流只看最高 BANDWIDTH，fallback 固定 best。选择低清仍可能耗费最高版本流量。
- **证据**：传 quality=480p，受控 master 同时声明低清和 1920×1080，高码率 high.m3u8 仍被请求。这里验证选流行为，没有声称合成素材本身为真实 1080p。
- **修复方向**：质量限制贯穿原生选择与 fallback，并正确关联音轨。单 MP4 只有一个版本是另一种合理限制。

### B08 · 中 · HLS 收尾失败后重新下载，暂停仍被禁用【静态确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:916`、`:541`、`:556`；`/Users/xxd/Playground/video-scrap/src/download_job.py:547`、`:339`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:1235`、`:1355`。
- **触发**：原生合并发出 finalizing 后失败，fallback 再次进入网络传输。
- **根因与影响**：监督器只把 finalizing progress 映射为状态，后来的 downloading 未恢复状态；界面也屏蔽 finalizing 后的 downloading。实际已经重下载，仍显示 Finishing，无法暂停。
- **修复方向**：允许明确的“后端重试 → downloading”阶段转换，协调监督器和界面恢复控制；本轮没有进行完整 GUI 动态复现。

### B09 · 中 · 保存路径含英文单引号时 HLS 合并失败【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/downloader.py:1396`、`:1399`。
- **根因与触发**：concat 列表以单引号包路径，内部单引号却按 `\\'` 转义；这不符合当前 FFmpeg concat 解析。实际任务临时目录位于用户保存目录下，所以上级目录带单引号也会触发。
- **证据与影响**：`directory'quoted/video.ts` 被解析成 `directory\\quoted/video.ts`，真实 FFmpeg 返回 254、Impossible to open。普通 HLS 可能额外 fallback 重下载，受保护 Range HLS 可能直接失败。
- **修复方向**：统一按 concat 语法编码路径，覆盖空格、单引号、反斜杠与 Windows 路径。

### B10 · 中 · 忽略 fsync 的存储错误后仍发布成功【故障注入复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:330`、`:333`、`:345`。
- **触发与根因**：`os.fsync(fd)` 报 EIO/ENOSPC 等实际存储错误，却被所有 OSError 通用捕获吞掉。
- **证据与影响**：注入 fsync 抛 EIO 后，下载仍返回成功并发布文件。延迟写入设备/网络卷可能在 flush 时才报错，因此不能把写调用成功等同于落盘成功；本轮没有制造真实硬盘故障。
- **修复方向**：区分明确可降级的“不支持”与实际 I/O 错误，实际存储失败必须阻止发布。

### B11 · 中 · 代理兜底转发的压缩响应头与正文不一致【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1458`、`:1696`、`:1719`、`:1724`。
- **触发**：总大小未知，进入 passthrough；上游返回 gzip 等压缩内容。
- **根因与影响**：复制压缩 Content-Encoding/Content-Length，却通过自动解压的 iter_content 发正文，造成二次解压或长度截断。
- **证据**：上游 gzip 为 43 字节，转发仍声明 gzip/43，却实际写出 1,200 字节解压正文。
- **修复方向**：统一 identity 策略，或转发原始 wire bytes；若转发解码正文，必须同步处理编码和长度响应头。

### B12 · 中 · 旧抓取结果覆盖新任务，来源地址和 Referer 串台【GUI 受控复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:210`、`:765`、`:777`、`:847`、`:863`、`:868`、`:935`。
- **触发**：A 静态抓取结束进入 WebEngine 后，输入 B 并按 Enter；或其他入口修改共享 `_current_url`，A 随后完成。
- **根因与影响**：按钮禁用没有阻止 Enter，忙碌判断只查普通 worker；回调使用共享当前 URL，缺少任务身份。A 的列表覆盖 B，A 视频却带 B 来源/防盗链头，还会恢复 B 正在使用的按钮。
- **证据**：真实离屏 MainWindow、假 worker/renderer：允许在渲染 A 时启动 B；A 回调得到 media=origin-a/clip.mp4，source=origin-b/watch，referer=origin-b/watch；B 仍 running 时按钮被重启用。
- **修复方向**：任务 ID、URL、会话参数随任务不可变传递，丢弃过期回调；按钮、Enter、工具栏统一判断任务状态。

### B13 · 中 · 重定向和 HTML base 导致相对媒体地址解析错误【Mock 复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:811`、`:827`、`:831`、`:838`、`:891`、`:941`。
- **触发与根因**：跟随重定向后仍用输入 URL 拼视频、封面、iframe 和 Referer，未使用最终 resp.url，也忽略 `<base href>`。
- **证据**：start.example/a 重定向至 final.example/dir/page，clip.mp4 错拼至 start.example/clip.mp4；声明 cdn.example/media/ 的 base 也被忽略。
- **影响与修复方向**：请求错误资源或被防盗链拒绝。分别保留输入来源、最终文档地址和 HTML base，按用途使用。现有 `test_scraper_flow.py:121` 已注明相对地址问题存在。

### B14 · 中 · blob/非 HTTP 媒体被当成直链，阻止正确回退【Mock 复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:823`、`:904`、`:929`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:800`。
- **触发与根因**：HTML 的 video src/data-src 未统一验证协议；`blob:https://...` 直接构造成已发现视频。
- **影响**：blob 仅在原页面浏览器有效，requests 无法下载；列表非空又会跳过后续 WebEngine 回退，形成“抓到了但不能播/下”的假成功。
- **修复方向**：全部入口统一验证 HTTP(S) URL；blob 用于触发浏览器解析，不能作为完整直链返回。

### B15 · 中 · 浏览器捕获单个 TS/M4S 被展示为完整视频【数据流确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:52`、`:154`；`/Users/xxd/Playground/video-scrap/src/scraper.py:974`。
- **根因与触发**：拦截器将 .ts/.m4s 与完整 MP4、manifest 混在同一集合，构造时全部标 is_direct。
- **影响**：播放几秒可能出现许多假“视频”；下载完成的只是一个片段，可能缺初始化段或音轨。
- **修复方向**：分类 manifest、完整资源和分片；分片用于追溯清单，不能直接作为完整影片候选。本轮用合成 m4s URL 确认构造结果，没有访问真实页面。

### B16 · 中 · 唯一正常视频名为 preview.mp4 时被无条件过滤【Mock 复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:208`、`:908`。
- **根因与影响**：特定站点 teaser 规则按 basename 全站过滤，正常网页唯一 `/preview.mp4` 也变成零结果。
- **修复方向**：结合已识别播放器和已存在的主视频降优先级，不能删除唯一可用候选。

### B17 · 中 · JS 解码替换语义错误，并输出未经清理的候选【Mock 复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/js_decoder.py:99`、`:103`、`:123`。
- **触发**：JavaScript 字符串 `.replace('TOKEN','')`，同一 token 出现多次。
- **根因与证据**：JS 字符串 replace 应只替换首次，Python 默认替换全部。aTOKENbTOKEN.mp4 应成为 abTOKEN.mp4，实际输出原始 aTOKENbTOKEN.mp4 和全部替换的 ab.mp4，缺少正确项。
- **额外影响**：没跟踪变量归属，附近其他变量的 replace 也可能被套用；候选 set 的顺序可能使错误原地址排在前面。
- **修复方向**：追踪同一变量操作链，正确实现被支持的 JS 语义，优先确定的最终结果并稳定排序。

### B18 · 中 · 非法 UTF-8 设置文件导致启动失败【临时文件复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/settings.py:64`、`:65`、`:110`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:164`。
- **根因与证据**：设置加载捕获 JSON/OSError，却不捕获 UnicodeDecodeError；临时 settings.json 内容 b"\xff" 导致 SettingsStore 构造抛异常。
- **影响与修复方向**：既有配置损坏即可阻止主窗口构造。增加编码错误容错，备份损坏文件并提供可追溯提示。

### B19 · 中 · 下载目录不可用时启动或按钮流程缺少恢复【静态确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:162`、`:1028`、`:1528`。
- **触发**：默认目录被同名文件占用、权限不足、只读磁盘、选择后外置盘卸载等。
- **根因与影响**：直接 mkdir 不捕获 OSError；构造时错误导致应用无法正常启动，按钮中错误作为未处理 Qt 回调异常外泄。是否整个进程终止取决于运行方式，未无条件判为必崩溃。
- **修复方向**：可恢复地提示路径和原因、允许重新选目录；目录可写性在实际操作前校验。

### B20 · 中 · embed 域名用子串判断，误收假域名/广告页【函数诊断复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:183`、`:503`、`:208`。
- **证据与影响**：youtube.com.evil.example/ad 被识别为已知 embed；后端失败仍可生成 fallback 项，污染列表并阻碍正确回退。placeholder 的 endswith 判断也缺点边界，会误伤 notartplayer.org 等地址。
- **修复方向**：使用 hostname 精确相等或带点子域比较；待解析 iframe 与已验证视频区分。没有证据把这项升级为盗取浏览器 Cookie 的漏洞。

### B21 · 中 · 排队任务没有开始前的单独取消入口【界面代码确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:456`、`:1038`、`:1040`、`:1075`、`:1208`。
- **触发与影响**：只显示两个活动下载行；第 3 个及以后只在内部队列，移除全局 Stop 后没有可见队列管理入口。误加任务只能等开始再取消，或退出应用；两个活动任务都暂停时，队列长期等待。
- **修复方向**：展示 queued 项并提供 Remove/Cancel，或提供明确队列管理入口。这是现有功能缺口，不能说活动任务取消整体失效。

### B22 · 中 · 普通站点浏览器登录会话未交给预览/下载【接口缺口确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:99`、`:165`、`:295`；`/Users/xxd/Playground/video-scrap/src/scraper.py:947`；`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:603`；`/Users/xxd/Playground/video-scrap/src/downloader.py:715`。
- **触发与根因**：媒体需要交互浏览器 Cookie；弹窗只返回 URL/title，构造的普通媒体没有会话，非 X 代理/下载没有这份 Cookie。
- **影响**：弹窗中可播放不代表回主界面仍可预览/下载，可能 403。X 的外部浏览器会话是独立实现，不属于此缺口。
- **修复方向**：受 domain/path/secure 等规则约束地传递当前媒体必要会话到代理和隔离进程；不能把整个浏览器 Cookie 广播给所有地址。具体登录站点未实网验证。

### B23 · 中 · JS 和交互页面结束后仍由主窗口保留【生命周期确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py:105`、`:129`、`:150`；`/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:205`、`:295`、`:301`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:847`、`:952`、`:969`。
- **根因与影响**：每次创建新的 Qt 子对象；成功、失败或超时只停 timer/发信号；对话框 accept/reject 隐藏而非销毁。覆盖 Python 属性不解除 Qt 父子所有权，旧网页可继续执行脚本、轮询或播放，反复操作累积 Chromium 资源。
- **修复方向**：成功、失败、取消、超时、关应用都停止并释放临时页面；共享 profile 单独交应用级对象拥有，以保留登录而不保留所有旧页面。未实网量测 RAM/流量增量。

### B24 · 低 · 非法 Range 可生成负 Content-Length【已复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:223`、`:1431`、`:1450`、`:1468`。
- **证据与影响**：16 字节资源请求 bytes=8-3，得到 206、Content-Range bytes 8-3/16、Content-Length -4，属于不合法 HTTP 响应。正常播放器通常不发送此请求，因此优先级较低。
- **修复方向**：校验起止、suffix 与范围满足性；返回合法拒绝响应，不生成负长度。

### B25 · 低 · 总大小未知时 suffix Range 丢失【静态确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1423`、`:1426`。
- **触发与影响**：bytes=-N 且大小探测失败，passthrough 调用未传原 Range，上游变整文件 GET，可能拖慢尾部元数据读取/seek。此处未实测具体播放器失败。
- **修复方向**：无法计算时保留并转发原合法 Range。

### B26 · 低 · 设置保存失败被隐藏，界面仍按已保存结束【静态确认】

- **位置**：`/Users/xxd/Playground/video-scrap/src/settings.py:94`；`/Users/xxd/Playground/video-scrap/src/gui/settings_dialog.py:167`。
- **触发与影响**：设置目录只读/存储写入失败。save 吞 OSError，当前内存已改、弹窗关闭，重启却丢失更改，用户无法判断持久化失败。
- **修复方向**：保存返回结果，保持界面可恢复并轻量提示；继续保留原子替换和失败时旧文件保护。

## 4. 条件性隐患与资源风险

### R01 · 中 · 多处网络读取没有字节/总时间边界

- **位置**：`/Users/xxd/Playground/video-scrap/src/scraper.py:449`、`:811`；`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1239`、`:1270`；`/Users/xxd/Playground/video-scrap/src/downloader.py:961`、`:1272`、`:1324`、`:1350`、`:860`；`/Users/xxd/Playground/video-scrap/src/gui/workers.py:89`。
- **条件与影响**：粘贴大型媒体直链时，yt-dlp 即便有结果，HTML 扫描仍全量读真正视频并转字符串/解析；普通 HLS 清单、AES 密钥、普通片段和缩略图也缺统一上限。16 片并发、chunks 再 join 进一步放大内存，异常持续响应可能导致 OOM。
- **证据级别**：静态读取路径确认；没有为了验证而耗尽机器内存。read timeout 不是整个响应的总期限。
- **建议**：文档/密钥严格小上限、检查类型和前缀；普通片段流式落私有文件；限制在途 Future 数与解码后大小。PNG decoder 的 64 MiB 限额只保护对应包装解码，不覆盖所有 HTTP 输入。

### R02 · 中 · 缓冲秒数不限制累计磁盘，直播旧片段没有淘汰

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:364`、`:463`、`:1007`、`:1878`。
- **条件与影响**：长时间播放/反复 seek 可缓存接近整部视频；直播滑动窗口不断新增 URL，旧资源保留至整个流释放，累计磁盘、索引和句柄增长。
- **证据**：两份不重叠的 5 片段直播窗口重写后仍保留 10 个片段；没有进行磁盘写满试验。
- **建议**：独立全局字节额度、剩余空间保护、窗口/LRU 淘汰；直播用 media sequence 识别过期资源。正常流释放确实有删除逻辑。

### R03 · 中 · 异常退出后的历史缓存和 staging 缺少安全回收入口

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:830`、`:900`；`/Users/xxd/Playground/video-scrap/src/download_job.py:443`、`:493`。
- **条件与影响**：强杀、崩溃、断电跳过正常 cleanup，可能留下 video_scraper_cache_* 或 .video-scrap-job-*；下次启动未看到应用级识别/回收。系统可能清理临时目录，但时间无保证。
- **建议**：目录记录所有者/存活标记，保守回收确认失活的缓存，提供占用/清理入口；不能直接删除全部同前缀目录。正常完成/取消的清理和明确报告失败应保留。

### R04 · 中 · HLS 预取失败无退避，可反复请求失效片段

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:690`、`:741`、`:748`、`:784`。
- **条件与影响**：窗口中片段快速返回 403/404/503，futures 异常被吞后立即重排，可能持续消耗网络/CPU，并触发服务端限流。没有做请求洪泛实测。
- **建议**：资源级指数退避、尝试次数/下一次重试时间；鉴权失效应提示重新解析，不无限快速重试。

### R05 · 中 · seek 后旧 HLS 预取任务仍留在队列

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:741`、`:748`、`:757`。
- **条件与影响**：大缓冲、短分片、多次远距离拖动时，只 break 等待循环、未取消旧 future，新窗口继续入队。8 个执行线程并不限制历史排队量，会下载不再需要的片段并延迟当前位置。
- **建议**：预取代次、取消旧待执行任务、限制 outstanding 数；执行前再检查所属窗口。静态调度路径确认，未做长时压力测试。

### R06 · 低 · 预取锚点只向前，不正确跟随回退/直播下标滑动

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1142`、`:1288`、`:704`。
- **影响**：退回未缓存区仍可能按历史最高下标预取；直播窗口重新编号也使位置失配。播放自身还能按需请求，不能因此断言必然不能播放。
- **建议**：当前 anchor 与历史最高位置分开，直播使用绝对序列与新清单重映射。

### R07 · 中 · 缩略图/抓取仍同步等待并强制终止 QThread

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/video_list.py:165`、`:175`；`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:734`；`/Users/xxd/Playground/video-scrap/src/gui/workers.py:48`、`:85`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:1602`。
- **条件与影响**：慢请求期间切换、重新抓取或退出；run 中没有 Qt 事件循环/中断检查，quit 和 requestInterruption 无法结束阻塞 I/O。列表在 GUI 顺序 wait，再 terminate，造成停顿；强停时锁/网络资源能否清理无保证。
- **边界**：未复现实际死锁/崩溃，不能写成必然崩溃。下载监督线程此前已使用独立进程清理，这里的问题是缩略图和抓取。
- **建议**：协作取消和总时限；切换先使旧结果失效、异步收尾并保留线程所有权，必要时抓取也隔离进程。

### R08 · 低 · 旧缩略图排队信号可能覆盖新视频封面

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:364`、`:371`、`:709`、`:730`。
- **条件与影响**：旧 ready/failed 已排进 GUI 队列时切视频；预览始终用 index=0，槽没有代次检查。停止线程不能撤回已排队信号，可能显示错封面/旧错误，不改变下载字节。
- **建议**：请求代次或视频身份校验，断开旧连接。列表已有代次防护，应保持；本项未做真实网络竞态复现。

### R09 · 中 · 手工 Authorization 可跨同主机不同协议/端口传播【构造复现】

- **位置**：`/Users/xxd/Playground/video-scrap/src/media_proxy.py:1855`、`:1858`。
- **条件**：顶层确实携带敏感 extra_headers，HLS 引用同 hostname 但不同 origin 的绝对地址。
- **证据与影响**：https://cdn.example/master.m3u8 上的 Bearer SAMPLE 被复制至 http://cdn.example:8080/plain.ts；令牌可能明文发送或交给另一端口服务。
- **建议**：敏感头比较规范化 scheme/hostname/effective_port，禁止 HTTPS 降级携带凭证。此结论不等于所有 Cookie 泄露；CookieJar 的 domain/path/secure 和不同 hostname 的过滤应保留。

### R10 · 中 · 并行 Range 没绑定同一资源版本

- **位置**：`/Users/xxd/Playground/video-scrap/src/parallel_downloader.py:364`、`:220`、`:244`。
- **条件与影响**：同 URL 下载过程中内容更新且总长相同，或 CDN 不同节点返回不同版本；长度/Content-Range 全对也可能拼出混合文件。
- **建议**：保存并校验强 ETag 等验证器，支持时用 If-Range/条件请求；版本变化时重启或选择一致传输。本轮未构造变化中的真实 CDN，属于协议条件风险。

### R11 · 低至中 · 根子进程异常退出可能留下尚未发现的 FFmpeg

- **位置**：`/Users/xxd/Playground/video-scrap/src/download_job.py:144`、`:443`、`:569`、`:586`。
- **条件与影响**：根下载进程刚创建后代便崩溃，监督器约 30ms 轮询还没记录到后代；孤儿被重挂后无法再从 root.children 找回。正常已发现进程的暂停取消检查不覆盖此窗口。
- **建议**：评估 Windows Job Object/POSIX 进程组及父死亡处理，继续严格限制只管理本任务进程。未动态复现此崩溃窗口。

### R12 · 低 · DownloadJob 状态更新与回调存在 API 层乱序窗口

- **位置**：`/Users/xxd/Playground/video-scrap/src/download_job.py:323`、`:339`、`:351`、`:363`。
- **条件与影响**：更新 state 后解锁、wake，再 emit，其他线程可能先发新状态，再到旧状态通知。当前 GUI 有旧状态防护，Qt 直接/排队连接也影响顺序，未证明用户按钮因此卡住。
- **建议**：统一事件发出线程或带状态序号；先验证实际调用顺序再改，不能把此项当作已复现暂停失效。

### R13 · 中/平台条件 · Windows 保留文件名及其他文件系统长度边界

- **位置**：`/Users/xxd/Playground/video-scrap/src/utils.py:47`；`/Users/xxd/Playground/video-scrap/src/downloader.py:102`、`:1567`。
- **事实与影响**：safe_filename 对 CON/NUL/AUX/COM1 原样返回，追加扩展名仍存在 Windows 设备名兼容问题；按字符截断也不能保证所有外接/网络文件系统的长度限制。
- **边界**：本轮确认 sanitize 输出，没有 Windows 实机文件创建证据。本机 APFS 成功创建 120 个汉字加 .mp4，不能声称中文标题在当前 Mac 必然过长。
- **建议**：平台保留名转义、尾随点/空格和总路径处理，失败时可恢复地缩短名称，并在目标文件系统验证。

### R14 · 低 · 非有限时长元数据缺少防御

- **位置**：`/Users/xxd/Playground/video-scrap/src/utils.py:21`。
- **证据与影响**：纯函数输入 NaN/Infinity 分别抛 ValueError/OverflowError，列表和预览调用可能受影响；正常 yt-dlp 通常提供正常数值，尚未证明真实网页会送入该输入。
- **建议**：转换前校验有限、非负，异常显示未知时长。

### R15 · 低 · 设置“线程安全”保证仅覆盖内存，订阅缺少解除

- **位置**：`/Users/xxd/Playground/video-scrap/src/settings.py:117`、`:122`、`:131`。
- **条件与影响**：多个线程并发 update 时，锁外 save/listener 可能旧值最后落盘/回调；subscribe 无去重/取消也会延长对象寿命。当前 UI 主要同线程更新、代理是长寿命单例，未确认生产事故。
- **建议**：序列化更新与持久化/通知，定义订阅生命周期；不宜只凭类名 thread-safe 就扩大保证范围。

### R16 · 低 · 源 URL 拼入 QLabel 富文本未转义

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py:517`。
- **条件与影响**：特殊引号/实体样式 URL 可能改变 href 或显示结构；没有证据说明可执行 JavaScript。
- **建议**：分别转义链接属性和显示文本，限制外链协议。标题、元信息已有 PlainText，应保持。

### R17 · 低 · 完整签名媒体 URL 进入日志

- **位置**：`/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:245`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py:751`。
- **条件与影响**：抓取含 token/signature 的媒体地址，再把日志/截图发给别人，可能暴露短期链接；未发现自动外发日志的行为。
- **建议**：日志隐藏签名 query/路径令牌，调试时提供明确的完整信息开关。

### R18 · 低 · 共享 requests.Session 的并发保证表述过强

- **位置**：`/Users/xxd/Playground/video-scrap/src/net.py:9`；`/Users/xxd/Playground/video-scrap/src/gui/workers.py:28`。
- **事实与边界**：连接池可并发不等于 Session 的全部可变状态都可任意并发；当前适配器主要只读，CookieJar 有自身保护，本轮没有证实实际数据竞态。
- **建议**：明确共享规则，避免并发修改默认 headers/auth/cookies；必要时每线程会话共用适配器策略。此项是设计约束，不是已确认文件损坏。

## 5. 维护、构建与验证改进

| 编号 | 位置 | 问题与建议 | 优先级/证据 |
|---|---|---|---|
| M01 | `/Users/xxd/Playground/video-scrap/requirements.txt:1` | 多数运行依赖仅有下限，新环境安装不保证复现现有版本。保留开发可升级能力，另记录并锁定发布环境，再有计划升级。不是断言最新版已有漏洞。 | 低，配置确认 |
| M02 | `/Users/xxd/Playground/video-scrap/tests/`；`/Users/xxd/Playground/video-scrap/README.md:252` | 测试覆盖具体，但未看到版本控制内的 CI；0.3.1 Windows 原生回归/成品报告仍缺。模拟 sys.platform/pwrite 不替代 NTFS、原生进程控制和打包运行。 | 中，验证缺口 |
| M03 | `/Users/xxd/Playground/video-scrap/scripts/build_macos.py:150`；对照 `/Users/xxd/Playground/video-scrap/scripts/build_windows.py:118` | Mac 生成报告必须有 Git 和 .git；从源 ZIP 打包可能已经产出 app/ZIP，却在记录提交时失败。Windows 有 git_state 容错，Mac 可统一。 | 低，静态确认，未实际打包复现 |
| M04 | `/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper-Windows.spec:40` | Windows EXE 没配置 VERSIONINFO，About 正确不代表文件属性里有 0.3.1。建议从单一版本源生成资源与 Mac 字段，减少手工重复维护。 | 低，配置缺口，未检查 Windows 成品属性 |
| M05 | `/Users/xxd/Playground/video-scrap/src/gui/main_window.py:67`、`:137`、`:1319`、`:1324`、`:1327` | 下载隔离后仍保留无实际调度作用的 lane 常量、字段与空方法；兼容参数仍向下传。后续先检查调用者，再清晰弃用，避免误以为共享限流仍有效。 | 低，静态确认 |
| M06 | `/Users/xxd/Playground/video-scrap/src/gui/main_window.py:783`、`:1532`、`:1536`；`/Users/xxd/Playground/video-scrap/src/gui/video_list.py:113` | 完成的抓取/缩略图 worker、关闭的 About/Preferences 未及时释放，由 Qt 父对象保留；比 Chromium 页面轻，但长会话对象累积。离屏反复构造并关闭 3 个 About 后，父窗口仍持有 3 个。建议 finished/deleteLater 与对话框释放策略。 | 低，部分对象保留已复现 |
| M07 | `/Users/xxd/Playground/video-scrap/src/downloader.py`；`/Users/xxd/Playground/video-scrap/src/media_proxy.py`；`/Users/xxd/Playground/video-scrap/src/gui/main_window.py` | 分别 1,806/1,882/1,615 行，协议、调度、状态和 UI 混杂，跨层修复容易不一致。建议先补关键行为约束，再小步提取 manifest 模型、传输、任务状态和视图；不建议立即大规模重写。 | 低，结构维护风险 |
| M08 | `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py:92` | 持久浏览器 profile 没有登录状态/浏览器缓存管理入口，且 UA 固定为 macOS Chrome124。可能影响隐私管理与站点兼容；未实网证实具体站点风控失败。 | 低，功能/兼容改进 |

## 6. 明确限制，不重复包装成新 Bug

1. 原生 HLS 下载读取一次清单快照，不刷新直播，也未按 ENDLIST 区分持续录制；不能承诺完整直播录制。位置：`/Users/xxd/Playground/video-scrap/src/downloader.py:948`、`:1177`、`:929`。
2. 暂停冻结本任务进程，不保证长期暂停后远端连接/签名 URL 仍有效；不支持退出应用后恢复。当前 README 已说明。
3. 两个暂停任务仍占两个槽位，这是明确队列设计；与 B21 没有队列移除入口分开判断。
4. Byte Range + MAP 初始化段及部分加密 I-frame Range 被主动安全拒绝。位置：`/Users/xxd/Playground/video-scrap/src/downloader.py:1156`、`:1165`。不能删除检查来换取表面兼容。
5. 自定义 Next/TanStack/数组 JS 只支持特定模式，不是通用 JavaScript 解释器。动态认证、DRM、任意脚本变体不能据此保证支持。
6. yt-dlp 抓取没有统一总体预算、socket_timeout/cancel；完整播放列表和 iframe 顺序解析可耗时较长。位置：`/Users/xxd/Playground/video-scrap/src/scraper.py:628`、`:748`。应结合 R01/R07 限额与取消设计处理。
7. 正常流释放/正常退出有缓存清理；发布后独立底层 .part 删除失败可按测试约定保留，GUI 还套私有 staging 清理，不能推断每次 GUI 下载必留 .part。位置：`/Users/xxd/Playground/video-scrap/tests/test_parallel_downloader.py:572`。
8. Mac ad-hoc 未公证、Windows 未签名是当前构建状态；不能把打包成功等同于所有系统无拦截、所有平台已验证。

## 7. 已核对有效的保护与排除的误报

- Range 起点、长度、总大小变化、异常编码已有校验；上游忽略 Range 返回 200 时按真实零偏移写缓存。此次新问题 B11/R10 属不同分支/实体一致性，不应笼统复报旧的“错误 206 字节仍写入”问题。
- 私有 staging 和唯一名称发布解决了旧的同名任务共用 .part；现有发布与取消有提交顺序，完成后的迟到取消不应删除有效成品。
- 取消前置标记、普通/X yt-dlp 取消检查、FFmpeg 注册锁、真实 QThread.finished 后释放、清理失败可见化都已存在。不能凭新发现的 fallback 状态问题认定整个暂停/取消功能无效。
- PNG 包装解析检查 CRC、截断、重复媒体块、IEND 与压缩输出上限，包装播放/预取共享 fetch 锁；未发现本轮相应确定边界缺陷。
- 全屏保留同一播放器、音频输出和 video widget；正常切换不重置播放源。音量和静音已有实际行为检查。
- X URL 主机和帖子路径验证、CookieJar 域过滤及 Cookie 不进入 repr 等保护仍有效。
- 配置采用同目录临时文件和 os.replace，正常写入的原子替换方案应保留；B18/B26/R15 是其他边界。
- 本机真实 FFmpeg 拒绝 HTTP HLS 引用 file://，返回 Protocol 'file' not on whitelist；没有证据把当前实现列为任意本地文件读取漏洞。
- 本机 APFS 实测可创建 120 个汉字加 .mp4；不采纳“UTF-8 超过 255 字节就在当前 Mac 必失败”的误报。
- 初步怀疑 WebEngine interceptor 必在 IO 线程并直接更新 GUI，核对当前 Qt6.11 实现后已排除：当前路径 InterceptOnUIThread 明确在 UI 线程调用。依据：[Qt6.11 官方源码](https://raw.githubusercontent.com/qt/qtwebengine/6.11/src/core/net/proxying_url_loader_factory_qt.cpp)。不据旧 Qt 经验报告当前确定崩溃风险。
- `/Users/xxd/Playground/video-scrap/src/scraper.py:947` 的 build_items_from_urls 是纯解析和对象构造，不在该函数中联网；没有把它误报为 GUI 同步网络卡顿。

## 8. 定向诊断证据摘要

以下均发生于本轮 2026-10-17（UTC+08:00）；没有保存每个子诊断的独立分钟时间，因此不编造逐条精确时刻。诊断临时媒体和设置已清理，结果为本轮工具返回记录摘要。

| 诊断 | 输入/方法 | 实际结果 | 对应项 |
|---|---|---|---|
| 空 HLS 中段 | 合成 3×1 秒，第二段 200 空正文，真实 FFmpeg | completed，成品仅 1 秒 | B01 |
| 外置 HLS 音轨 | 合成 video-only，master 带 AUDIO | 只请求视频，输出无 Audio | B02 |
| 文件句柄 | 独立进程软额度 128，200 片段清单 | 124 子缓存时 EMFILE，缓存媒体 0 字节 | B03 |
| 滑条鼠标 | 真实离屏 Qt，点轨道/移动/释放 | value 停在 66，slider_down 一直 True | B04 |
| 无扩展名子清单 | 进程内构造普通 master | 两个子项均未标 manifest | B05 |
| 清晰度路由 | quality=480p，多版本受控 master | 仍请求高码率 high.m3u8 | B07 |
| concat 路径 | 临时单引号目录，真实 FFmpeg | 错路径，返回 254 | B09 |
| fsync 存储错误 | 注入 errno.EIO | 仍返回成功/发布 | B10 |
| gzip passthrough | 43 字节 gzip / 1,200 字节解码正文 | 错误压缩头和长度被保留 | B11 |
| A/B 抓取任务 | 离屏真实界面，假 worker/renderer | A 视频使用 B source/referer；按钮提前可用 | B12 |
| HTML 分类/解析 | Mock redirect/base/blob/preview | 错拼 URL、blob 假项、唯一视频被过滤 | B13/B14/B16 |
| JS replace | 合成 token 重复字符串 | 原 URL 与全替换 URL，缺正确 JS 结果 | B17 |
| 配置编码 | 临时 b"\xff" settings.json | UnicodeDecodeError | B18 |
| embed hostname | youtube.com.evil.example/ad | 被识别为已知 embed | B20 |
| 非法 Range | total=16，bytes=8-3 | 206，Content-Length=-4 | B24 |
| 直播资源保留 | 两批不重叠 5 片段清单 | 仍保留 10 个旧/新子项 | R02 |
| 手工敏感头 | HTTPS 父，HTTP 同主机异端口子项 | Bearer SAMPLE 被继承 | R09 |
| 元数据边界 | NaN/Infinity duration | ValueError/OverflowError | R14 |
| 轻量对话框保留 | 3 次 About 构造/reject | 父窗口仍有 3 个 About 子对象 | M06 |

## 9. 现有测试的价值与缺口

29 个测试文件均已阅读全文；本轮没有全套重跑，也没有把历史通过数复制成当前通过结果。

有效覆盖包括：Range 错响应和长度、并行短写、缺 pwrite 的互斥写入、同名文件保护、原始音频编码包保留、MP3/M4A、取消和最终发布竞态、spawn 进程控制、下载线程所有权、全屏/音量、面板布局、设置原子保存、X 多视频和会话域过滤、PNG 包装边界。

本轮新问题未被这些正常/局部用例充分覆盖：普通 HLS 空中段、原生成功路径的外置音轨、HLS 质量限制、大清单 FD 上限、普通无扩展名子清单、slider 真鼠标释放、A/B 抓取串台、重定向相对 URL/base、通用登录会话转交、页面销毁、非法 UTF-8、目录失效、fsync 故障、同 hostname 跨 origin 凭证、实体版本变化、长期直播缓存和预取队列。

验证结果不能跨边界推导：Fake yt-dlp 不证明真实站点，Mock Windows 接口不证明 Windows 原生，Mock 硬链接失败不证明所有卷，线程并发命名用例不自动覆盖跨机器共享盘。部分 Range 测试用字节拼接替代 remux，因此真实 FFmpeg 的空片段行为可能绕过现有测试。

测试会修改临时 sys.platform、sys.modules、CWD、全局 os 接口等；现有顺序/独立脚本用法有上下文恢复。本轮未确认实际污染，但未来不能直接改成同进程多线程并行跑而不做隔离。

## 10. 建议修复顺序

1. **先保证成品正确和应用可用**：B01、B02、B03；分别补空中段、正常分离音轨和大清单资源边界检查。
2. **恢复常用交互和状态一致性**：B04、B08、B12、R07、B18、B19；逐项修，不同时重写下载核心。
3. **修协议与抓取分类**：B05–B07、B11、B13–B17、B20、B22、R09；保持 Range 和凭证隔离保护。
4. **控制长会话资源**：B23、R01–R06、R08；加缓存额度、回收、预取代次/退避和明确生命周期。
5. **完善失败和平台边界**：B09、B10、B21、B24–B26、R10–R18，补 Windows 原生/目标文件系统验证。
6. **最后做维护整理**：M01–M08；有行为检查后再逐步拆模块。开发依赖升级、直播持续录制、可视队列/缓存管理属于需要明确产品范围的工作。

以上是建议，尚未执行修复。本次没有获得新一轮提交/推送指令；后续若获授权推送，继续遵循 AGENTS.md 的版本递增规则。

## 11. 全文件审查清单

以下为本轮核对的全部 67 个 Git 跟踪文件；路径均为绝对路径。代码和测试阅读全文；配置和文档核对内容；Logo 核对资源引用。构建目录、虚拟环境、第三方源码和产物不计入逐行清单。


| 文件完整路径 | 范围/检查内容 |
|---|---|
| `/Users/xxd/Playground/video-scrap/.gitignore` | 构建、下载、虚拟环境与本地文件忽略规则 |
| `/Users/xxd/Playground/video-scrap/AGENTS.md` | 协作、Release 文案与推送前版本约定 |
| `/Users/xxd/Playground/video-scrap/Logo.png` | 资源存在与代码引用检查（二进制图片不属于逐行代码） |
| `/Users/xxd/Playground/video-scrap/README.md` | 当前版本、运行/打包步骤与限制 |
| `/Users/xxd/Playground/video-scrap/WINDOWS_BUILD.md` | Windows 构建与原生验证步骤 |
| `/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper-Windows.spec` | Windows 收集、图标、依赖和版本资源 |
| `/Users/xxd/Playground/video-scrap/app_bundle/VideoScraper.spec` | macOS 收集、版本、签名与系统要求 |
| `/Users/xxd/Playground/video-scrap/app_bundle/runtime_hook.py` | 冻结应用 FFmpeg 路径 |
| `/Users/xxd/Playground/video-scrap/main.py` | 启动、单实例接入、下载退出收尾 |
| `/Users/xxd/Playground/video-scrap/requirements-build.txt` | 打包依赖锁定 |
| `/Users/xxd/Playground/video-scrap/requirements.txt` | 运行依赖及版本约束 |
| `/Users/xxd/Playground/video-scrap/scripts/build_macos.py` | Mac 资源、兼容版本、打包和可追溯报告 |
| `/Users/xxd/Playground/video-scrap/scripts/build_windows.py` | Windows 架构、资源、打包和可追溯报告 |
| `/Users/xxd/Playground/video-scrap/scripts/smoke_app.py` | 成品离线检查、合成媒体、异常与资源清理 |
| `/Users/xxd/Playground/video-scrap/src/__init__.py` | 应用版本来源 |
| `/Users/xxd/Playground/video-scrap/src/download_job.py` | 子进程监督、暂停恢复、IPC、发布与清理 |
| `/Users/xxd/Playground/video-scrap/src/downloader.py` | 直链、HLS、音频、yt-dlp、FFmpeg 与命名 |
| `/Users/xxd/Playground/video-scrap/src/ffmpeg.py` | FFmpeg 发现及跨平台路径 |
| `/Users/xxd/Playground/video-scrap/src/gui/__init__.py` | GUI 包初始化 |
| `/Users/xxd/Playground/video-scrap/src/gui/about_dialog.py` | Logo、版本、外链和对话框行为 |
| `/Users/xxd/Playground/video-scrap/src/gui/icons.py` | 图标绘制、缓存、组合框/数值框事件 |
| `/Users/xxd/Playground/video-scrap/src/gui/interactive_scraper.py` | 持久 profile、请求捕获、提取和弹窗生命周期 |
| `/Users/xxd/Playground/video-scrap/src/gui/js_renderer.py` | 后台渲染、超时、提取与生命周期 |
| `/Users/xxd/Playground/video-scrap/src/gui/main_window.py` | 布局、抓取回退、下载队列、状态与退出 |
| `/Users/xxd/Playground/video-scrap/src/gui/preview_panel.py` | 播放源、滑条、全屏、音量与缩略图 |
| `/Users/xxd/Playground/video-scrap/src/gui/settings_dialog.py` | 设置输入、主题、保存和取消 |
| `/Users/xxd/Playground/video-scrap/src/gui/style.py` | 主题、Qt 调色板、控件样式与布局边界 |
| `/Users/xxd/Playground/video-scrap/src/gui/video_list.py` | 列表、选择、封面并发与清理 |
| `/Users/xxd/Playground/video-scrap/src/gui/workers.py` | 抓取/缩略图线程与隔离下载桥接 |
| `/Users/xxd/Playground/video-scrap/src/hls_payload.py` | PNG 包装、CRC、解压与输入边界 |
| `/Users/xxd/Playground/video-scrap/src/js_decoder.py` | 受限 JavaScript 字符串解码 |
| `/Users/xxd/Playground/video-scrap/src/media_proxy.py` | HTTP 代理、HLS 改写、缓存、预取与资源生命周期 |
| `/Users/xxd/Playground/video-scrap/src/net.py` | 共享 Session、连接池与重试 |
| `/Users/xxd/Playground/video-scrap/src/parallel_downloader.py` | 并行 Range、文件写入、唯一发布和回退 |
| `/Users/xxd/Playground/video-scrap/src/scraper.py` | 提取编排、X、多视频、HTML 与数据识别 |
| `/Users/xxd/Playground/video-scrap/src/settings.py` | 设置加载、保存、并发和通知 |
| `/Users/xxd/Playground/video-scrap/src/single_instance.py` | 锁、IPC、启动竞态、唤起与清理 |
| `/Users/xxd/Playground/video-scrap/src/utils.py` | 文件名、URL 与元数据格式化 |
| `/Users/xxd/Playground/video-scrap/tests/test_audio_download_controls.py` | 全文检查测试输入、断言、隔离和覆盖边界；6 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_audio_downloads.py` | 全文检查测试输入、断言、隔离和覆盖边界；16 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_collapsible_panels.py` | 全文检查测试输入、断言、隔离和覆盖边界；10 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_cancellation.py` | 全文检查测试输入、断言、隔离和覆盖边界；19 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_controls.py` | 全文检查测试输入、断言、隔离和覆盖边界；15 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_jobs.py` | 全文检查测试输入、断言、隔离和覆盖边界；21 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_name_reservation.py` | 全文检查测试输入、断言、隔离和覆盖边界；10 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_download_shutdown.py` | 全文检查测试输入、断言、隔离和覆盖边界；4 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_ffmpeg_resolver.py` | 全文检查测试输入、断言、隔离和覆盖边界；18 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_hls_byte_ranges.py` | 全文检查测试输入、断言、隔离和覆盖边界；18 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_hls_payload.py` | 全文检查测试输入、断言、隔离和覆盖边界；18 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_hls_preview_master.py` | 全文检查测试输入、断言、隔离和覆盖边界；10 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_media_proxy_ranges.py` | 全文检查测试输入、断言、隔离和覆盖边界；13 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_next_data_ev.py` | 全文检查测试输入、断言、隔离和覆盖边界；4 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_parallel_downloader.py` | 全文检查测试输入、断言、隔离和覆盖边界；21 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_preferences_dialog.py` | 全文检查测试输入、断言、隔离和覆盖边界；9 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_preview_controls.py` | 全文检查测试输入、断言、隔离和覆盖边界；9 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_preview_formats.py` | 全文检查测试输入、断言、隔离和覆盖边界；10 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_scraper_config_hls.py` | 全文检查测试输入、断言、隔离和覆盖边界；4 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_scraper_flow.py` | 全文检查测试输入、断言、隔离和覆盖边界；4 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_settings.py` | 全文检查测试输入、断言、隔离和覆盖边界；3 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_single_instance.py` | 全文检查测试输入、断言、隔离和覆盖边界；12 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_tanstack_ev.py` | 全文检查测试输入、断言、隔离和覆盖边界；13 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_ui_layout.py` | 全文检查测试输入、断言、隔离和覆盖边界；5 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_wrapped_hls_download.py` | 全文检查测试输入、断言、隔离和覆盖边界；9 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_wrapped_hls_proxy.py` | 全文检查测试输入、断言、隔离和覆盖边界；9 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_x_downloader.py` | 全文检查测试输入、断言、隔离和覆盖边界；4 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_x_preview.py` | 全文检查测试输入、断言、隔离和覆盖边界；3 个 test_ 定义；本轮未全套运行 |
| `/Users/xxd/Playground/video-scrap/tests/test_x_scraper.py` | 全文检查测试输入、断言、隔离和覆盖边界；6 个 test_ 定义；本轮未全套运行 |

## 12. 本轮操作与交付状态

- 文件清单、Git 状态和日志采用合并读取，避免反复启动查询。
- AST 只解析源码，不执行测试；定向诊断仅运行报告中写明的合成/Mock/离屏输入。
- 全部新发现保持待修复状态；验证边界已逐项标注。
- 本次新增文件：`/Users/xxd/Playground/video-scrap/code_audit_2026-10-17.md`；没有覆盖日记或其他历史报告。
- 没有更改应用源码，没有重新打包，没有 commit/push。
