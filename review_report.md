# 《技术方案.md》技术评审报告

## 1. 评审信息

- 评审对象：仓库根目录《技术方案.md》，版本 v0.1，共 792 行。
- 评审日期：2026-08-27。
- 评审范围：总体架构、阶段职责与依赖、技术选型、JSON 数据契约、状态机与断点续跑、错误处理与降级、测试与验收、开发工期、风险与运维边界。
- 评审方式：逐行通读、文档内部一致性检查、目标主机只读环境核验、官方技术资料核验，以及不落盘的 FFmpeg 参数冒烟测试。
- 仓库证据边界：评审开始时仓库可见业务文件只有《技术方案.md》，没有实现代码、Schema、测试、样例或基准结果。因此，本报告可以确认文档中的逻辑错误和环境事实，但不能把尚未实现的性能、质量、稳定性或成本指标视为“已验证”。

## 2. 总体结论

**评审结论：不通过。**

8 阶段流水线的宏观方向是合理的：以阶段产物解耦解析、理解、讲稿、分镜、素材、合成和质检，采用本地与云端 Provider 混合，并以 ffmpeg 作为确定性合成核心，这些方向都可以保留。

但当前 v0.1 不能作为第 6、792 行所称的“零上下文可开发、冲突时以本文档为准”的唯一开发依据。主要原因是：

1. 合成先烧字幕、字幕后生成，且字幕和质检都叫 P7，现有阶段图不能唯一执行或恢复。
2. P3 只从 150–300 字总摘要和少量要点扩写讲稿，无法支撑 30 页报告的事实保真。
3. 所谓“完整 JSON Schema”实际上只是一个 JSON 实例；P5 还会覆写 P4 的已完成产物，直接破坏阶段哈希语义。
4. 状态机没有完整状态转移、原子提交、并发锁、逐项 checkpoint、配置和模型指纹，也不复核已完成产物。
5. LLM 分块按字符而非 token，默认上下文和 12 GB 显存并发条件未落实；当前配置很可能在长文档核心路径上溢出或退化。
6. FFmpeg 示例中 fade/afade 的时间算式可复现地报错，且命令没有实现竖屏、前后留白和承诺的 H.264 High 兼容规格。
7. LLM 跳章和 TTS 静音仍可能进入“完成”，会生成技术上可播放但业务内容缺失的伪成功成片。
8. edge-tts、FAL 和自动云 LLM 降级会把内容发往第三方，但方案没有隐私模式、授权、留存或日志边界。
9. FAL 所写“FLUX.2 Klein”与实际 model_id fal-ai/flux 不一致；端点会改变请求 Schema、能力、返回结构和计费，不能留到实现时猜测。
10. 字幕验收承诺平均偏差小于 300 ms，却用 ASR 回读替代 TTS 原生边界/强制对齐，也没有可复现的真值和分位数测量。
11. 真实 Provider E2E 被标为可选，无法验证音色、模型端点、队列下载、CUDA、字体及崩溃恢复，不能支撑发布级验收。

上述 P0 全部修订并通过契约测试、真实 Provider 冒烟测试、故障注入和恢复测试后，方案才适合重新评审；届时可望达到“有条件通过”。

## 3. 证据与环境核验摘要

### 3.1 已确认的本机事实

- Ollama 版本为 0.32.15；本机存在 Qwen3:14b，模型信息显示 14.8B、Q4_K_M、文件约 9.3 GB、模型最大上下文 40960。
- GPU 为 NVIDIA GeForce RTX 5070，显存 12227 MiB。
- OLLAMA_CONTEXT_LENGTH、OLLAMA_NUM_PARALLEL、OLLAMA_KV_CACHE_TYPE、OLLAMA_FLASH_ATTENTION 在本轮检查的进程、用户和机器环境中均未设置；因此文档中的并行和上下文假设当前至少没有由这些环境变量落实。
- 本机 ffmpeg 为 8.1.2 full build，并包含 libx264、AAC、libass、ass、subtitles 和 zoompan。方案要求“锁定 6.x”，但安装指令使用未固定版本的 winget，当前环境已经证明该要求不可复现。
- 对 ffmpeg 8.1.2 执行不落盘测试时，zoompan 的 d=25*1 可以执行；fade 的 st=1-0.3 和 afade 的 st=1-0.3 均返回“Unable to parse st option value ... as duration”。因此附录中的 DUR/TOTAL 若只做字符串替换，命令会失败。

### 3.2 需要明确标为“未验证”的主张

- Qwen3:14b 在该主机上并行 2 路、同时保留 faster-whisper small 时的显存占用、吞吐和稳定性。
- faster-whisper small 在中文 TTS 上达到 5–10 倍实时速度及小于 300 ms 对齐误差。
- 5000 字文档在默认云图像链路上的端到端 P50/P95 是否小于 10 分钟。
- FLUX 图像的风格一致性、无乱码文字率、实际单位成本和批量限流行为。
- 三类典型输入的结构解析正确率、讲稿事实准确率和批处理 10 个文件的稳定性。

## 4. 架构与技术选型评价

| 项目 | 评价 | 关键判断 |
|---|---|---|
| 8 阶段流水线 | 方向合理，当前边界不成立 | P0–P7 的线性主链适合单机 MVP，但 P6/P7 字幕依赖、P5 并行分支和 P7 发布门禁必须重画为可恢复 DAG。 |
| PyMuPDF / python-docx | 有条件适用 | 适合作为低层提取器，不等于可靠的语义版面解析器；多栏、阅读顺序、页眉页脚、跨页表格、混合 OCR 和 DOCX 自定义结构均需补充。 |
| Ollama Qwen3:14b | 可作为本地基线 | 本地成本和隐私优势明确，但应使用结构化输出、精确 token 预算、模型摘要/版本固定和显存调度，不能把字符估算与并行 2 当成已验证能力。 |
| edge-tts | 适合原型，不宜单独承担生产默认 | 它调用在线 Edge TTS 服务；无 API key 不等于离线、稳定 SLA 或商业条款已明确。其时间边界元数据应直接用于字幕。 |
| FAL FLUX.2 Klein | 适合生成式插画，不适合替代事实图表 | 精确模型端点写错或未固定；对报告图表、数字和品牌素材应优先使用原文图片、页裁切或确定性模板。 |
| ffmpeg | 选型正确 | 性能、可控性和可重复性均优于把 moviepy 作为生产核心；但当前命令模板、时间轴、横竖屏、编码格式和最终响度流程需要重写。 |
| faster-whisper | 适合作为 ASR 回读和兜底 | 它可输出词时间戳，但不等同于对指定原文的强制对齐；小模型加字符相似度不能天然保证小于 300 ms。 |
| asyncio + state.json | 可用于单机，但当前持久化设计不合格 | 无需立即引入 Prefect，但必须补齐原子提交、跨进程锁、远端请求恢复、逐项 checkpoint 和状态不变量。 |
| JSON Schema | 当前不合格 | 文档未给出可执行 Schema，也没有版本、严格字段、跨文件语义校验或迁移策略。 |

## 5. P0：必须修改，否则方案有硬伤

### P0-01：重画字幕、合成和质检的依赖关系，并统一阶段编号

**问题**

总体图把 P6 定义为“合成与字幕”、P7 定义为“质检与交付”；详细设计却先在 P6 合成器中烧录 ASS，再定义位于其后的“P7 字幕”，随后又定义另一个“P7 质检”。目录结构只有 p6_compose.py 和 p7_qc.py，没有字幕模块。字幕文件未生成时无法先烧录，状态机也无法唯一表示字幕失败的恢复点。

**依据**

- 《技术方案.md》L141–L143、L159–L160：P6 合成与字幕，P7 质检。
- L337–L348：P6 合成流程第 5 步已要求烧录字幕。
- L366–L393：字幕和质检均标为 P7。
- L461–L462：模块目录没有字幕模块。

**必须修改**

- 若坚持 8 阶段，明确 P6 内部有可持久化的有序子阶段：P6a 生成/对齐字幕 → P6b 场景与整片合成 → P6c 烧录或封装；P7 只负责 QC 和原子发布。
- 若改成 9 阶段，则用 P6 字幕、P7 合成、P8 QC，并同步所有表格、状态、目录、CLI、Schema 和验收用例。
- no-burn-subs 只能跳过烧录，不能跳过 SRT/ASS 生成。
- 最终文件先写 staging；只有 P7 硬门禁通过后才能晋升为可交付 output.mp4。

### P0-02：消除 P2 到 P3 的事实信息瓶颈，建立源文档可追溯讲稿

**问题**

P2 的输出只有 150–300 字全文摘要、最多 10 个要点和包含章节名、section ID、字数、计划场景数的 chapter_plan；P3 只接收这个 Summary，Prompt 也只注入这三项。对于 30 页报告，P3 无法从如此压缩的信息恢复细节，只能遗漏或创造事实。AC-04 仅抽听一条也无法证明讲稿与原文一致。

**依据**

- L124：典型场景是一份 30 页技术报告生成 5 分钟解说。
- L238–L255：summary.json 没有逐章事实、证据文本或来源位置。
- L257–L280、L736–L741：P3 只使用 Summary 中的摘要、要点和计划。
- L667：只人工抽听 1 条判断无事实错误。

**必须修改**

- parsed.json 中每个块保留 page、bbox、reading_order、OCR confidence 和稳定 block_id。
- summary.json 增加逐章/逐块摘要、数字/日期/专名事实表、source_block_ids 和覆盖记录。
- P3 按场景回取相关源块，在 script.json 的每个 scene 中记录 source_block_ids/source_pages；禁止只从总摘要二次扩写。
- 对数字、日期、专名、单位和否定关系做确定性一致性检查；章节覆盖不足或无证据主张进入 needs_review 或 failed。
- 医疗、法律、金融、合规等高风险公开发布场景不得承诺完全无人审核。

### P0-03：提供真正、版本化的 JSON Schema，并保持阶段产物不可变

**问题**

“核心 JSON Schema（完整示例）”展示的是 scenes.json 数据实例，不是 Schema：没有 type、properties、required、enum、范围或额外字段策略。实例中的 $schema 被写成项目相对路径，但 $schema 在标准 Schema 文档中用于声明方言，并不会让普通数据实例自动加载本地校验文件。与此同时，P5 会原地覆写 P4 已产出的 scenes.json，导致 P4 的 output_hash 在下游执行后改变。

**依据**

- L164–L165：宣称数据契约驱动、阶段产物哈希和独立重跑。
- L286–L300：P4 产出 scenes.json。
- L321、L333、L335、L487：P5 回写同一个 scenes.json。
- L499–L524：所谓完整 Schema 实为实例，且只笼统声称会校验。
- JSON Schema 官方入门明确区分 $schema 方言、$id 和实例验证规则。

**必须修改**

- 为 manifest、state、parsed、summary、script、scene_plan、assets_manifest、subtitles、render_manifest、qc_report 分别提供 Draft 2020-12 Schema。
- Schema 至少定义 $schema 方言 URI、稳定 $id、schema_version、required、enum、数值边界、路径格式、严格额外字段策略以及可为空条件。
- P2、P3、P4 的 LLMProvider 先做能力协商：支持严格 Schema 约束输出时优先使用；仅支持 JSON mode 或纯文本时，使用 JSON-only Prompt、严格解析、Pydantic/JSON Schema 校验和有上限的纠错重试。重试后仍不合格必须失败或转人工，不得靠“截取/补全”静默猜修业务字段。
- 阶段产物不可变：P4 写 scene_plan.json，P5 写 assets_manifest.json，P6 写 subtitles 和 render_manifest，P7 写 qc_report。可生成 resolved_scenes.json 作为只读合并视图，但不得覆写上游产物。
- JSON Schema 之外增加语义校验：ID 唯一、引用集合完全匹配、路径受限于 Job 根、文件存在、内容哈希和媒体探测通过。

### P0-04：把断点续跑改成保证边界明确的进程崩溃安全、并发安全状态机

**问题**

当前 resume 只找第一个非 done 阶段，并只看其上游哈希；全部 done 就直接返回。它不会复核已完成文件是否丢失、截断或不符合 Schema，也不会感知音色、风格、画幅、Prompt、模型、代码和工具版本变化。示例 state 顶层 phase 是 composing，但 P2 却 failed。全文没有原子写、提交顺序、作业锁、缓存键锁、进程崩溃或同一 Job 双 resume 的规则。

**依据**

- L165、L169：承诺哈希断点和 state.json。
- L321、L333、L335：P5 并行后回写共享 JSON。
- L526–L547：状态示例自相矛盾，恢复规则不复核 done 产物。
- L557–L563：多项配置会改变产物，但未纳入失效指纹。
- L565、L572–L573：batch/watch 可并发，未定义同 Job 互斥。

**必须修改**

- 定义状态枚举和不变量，例如 pending → running → committing → succeeded / succeeded_with_warnings / failed / cancelled；succeeded → invalidated → pending。
- 删除可漂移的顶层 phase，或仅由阶段状态推导；上游未成功时下游不得运行。
- 每阶段保存 stage_fingerprint：有序输入哈希、本阶段有效配置、Prompt 哈希、Provider/模型及修订、pipeline/code 版本、Schema 版本和相关工具版本。
- resume 从 P0 按依赖图复核全部已完成阶段的产物清单、实际 SHA-256、Schema、语义和指纹，从最早 dirty 节点级联失效。
- 所有 JSON/媒体先写到与目标位于同一文件系统的唯一临时文件，按 flush → os.fsync → close → os.replace 提交；state 和 artifact_manifest 使用相同协议。多文件阶段以最后提交的 artifact_manifest 作为唯一对外可见提交点，消费者忽略未被该 manifest 引用的文件。
- resume 必须处理“artifact_manifest 已提交但 state 尚未更新”的窗口：重新验证 manifest 后补记 succeeded；若 manifest 未提交，则清理或隔离孤儿临时文件并重跑。本文档最多承诺进程崩溃安全；若要承诺断电持久性，还需按目标文件系统实现并验证目录/句柄同步，不得仅凭 os.replace 宣称可证明。
- 每个 Job 使用跨进程排他锁与 revision；每个全局缓存键使用独立锁。P5 各任务写独立结果，由单写者确定性汇总。
- P5 按 scene_id × asset_kind 保存逐项 checkpoint、attempt、Provider request_id、错误和缓存键；中断后只重做未完成项。
- 明确人工修改中间产物的接受流程和 revision，不能既声称“可审可改”又把所有改动一律当成损坏。

### P0-05：改用精确 token 预算和资源调度，不能按 12000 字符及并行 2 硬编码

**问题**

文档把 12000 字符估成约 6000 token，但中文字符到 token 的比例依模型和内容变化，必须由实际 tokenizer 计算。更关键的是，Ollama 官方说明小于 24 GiB 显存的默认上下文为 4K，且内存随并行数 × 上下文长度增长。当前 12 GB GPU、9.3 GB 量化模型和 OLLAMA_NUM_PARALLEL≥2 的组合没有基准依据；环境检查还显示相关变量未设置。

**依据**

- L251–L253：固定按字符切块。
- L406、L439–L440、L634：Qwen3:14b、本地共存和并行至少 2 的硬结论。
- Ollama 官方 Context length 页面：低于 24 GiB 显存默认上下文为 4K；官方 FAQ：默认上下文为 4096，且内存按 NUM_PARALLEL × CONTEXT_LENGTH 增长。
- 当前主机：RTX 5070 12227 MiB；Qwen3:14b Q4 模型约 9.3 GB；相关环境变量未设置。

**必须修改**

- 以目标模型 tokenizer 计算 input token，并为 system prompt、Schema、输出和安全余量预留预算；超限继续细分而不是截断。
- 在 config 和 manifest 中固定 num_ctx、max_output_tokens、model digest、量化、temperature、seed 和结构化输出模式，并在启动时 preflight。
- 12 GB 单卡默认从 OLLAMA_NUM_PARALLEL=1 起步；只有目标机器基准证明足够时才升到 2。
- Ollama、faster-whisper 和本地 ComfyUI 通过全局 GPU 资源调度器互斥或限额；阶段切换时可用 keep_alive=0 卸载不需要的模型。
- 对 OOM、CPU offload、503 队列过载定义明确的降级或失败规则，不能静默降低性能后仍宣称满足 10 分钟 SLA。

### P0-06：重写 FFmpeg 时间轴和命令模板，修复已复现的非法参数

**问题**

示例把 DUR 代入 st=DUR-0.3，把 TOTAL 代入 st=TOTAL-3；fade/afade 的 st 接受时间长度，不接受这种算术字符串，本机 ffmpeg 8.1.2 已复现失败。命令还用 -t DUR 截断到音频实测时长，既没有开头留白，也没有实现“音频时长 + 前后留白”；scale 和输出被硬编码成 16:9，竖屏不可用。缺少 yuv420p、明确 profile、音频采样率和声道约束，也无法保证 H.264 High 交付。

**依据**

- L117：承诺 H.264 High、1080p 横竖屏。
- L343–L359：时间轴和单场景命令。
- L558、L605：9:16 必须可用。
- L769–L781：BGM fade 和最终编码命令。
- 本轮不落盘测试：fade/afade 的 st=1-0.3 均返回 Invalid argument。

**必须修改**

- 在 Python 中预先计算 narration_offset_s、lead_s、trail_s、scene_duration_s、fade_out_start_s、bgm_fade_start_s，并把纯数值传给 FFmpeg。
- 用参数数组调用子进程，禁止 shell 拼接；对 concat、ASS、Windows 盘符、引号和滤镜特殊字符建立专用转义和测试。
- 为 16:9 与 9:16 分别参数化画布；采用等比 scale + crop/pad，禁止无条件拉伸到 3840×2160。
- 场景编码统一 codec、profile、pix_fmt=yuv420p、分辨率、fps、time base、音频 sample rate/channels，满足 concat demuxer 的同流约束。
- 把 -profile:v high、兼容 level、48 kHz 音频等交付约束写入并由 ffprobe 验证。
- 单元测试不能只断言命令字符串；必须对短 lavfi/fixture 实际执行每个滤镜和横竖屏集成命令。

### P0-07：核心内容和口播必须失败关闭，P7 必须成为交付门禁

**问题**

LLM 失败允许跳过章节，TTS 失败允许静音占位，但项目目标是“带口播”的内容视频。当前 QC 甚至允许一个超过 2 秒的静音段，placeholder 只报告不阻断，阶段全部 done 即视为完成。这会把缺章、无声或时间轴无定义的视频当作成功。

**依据**

- L8、L36、L87、L93：目标、章节覆盖和字幕同步要求。
- L322、L579–L583：TTS 静音和 LLM 跳章降级。
- L386–L391、L547、L675：QC 与完成规则不足以阻止伪成功。

**必须修改**

- 核心链路状态区分 succeeded、succeeded_with_warnings、needs_review、failed，并定义 CLI 退出码。
- LLM 不得跳章后正常交付；重试或经授权切换 Provider 后仍失败就终止或等待人工复核。
- TTS 失败按主 Provider → 明确配置的第二 TTS → failed/needs_review 处理；静音只能用于显式 preview 模式，不能用于正式交付。
- 图像可允许占位，但需设置最大占位率和连续占位限制；audio_failed、chapter_skipped、证据覆盖不足必须是硬失败。
- P7 对可播放性、完整性、音频、字幕、编码和事实覆盖执行硬门禁；失败文件保留在 staging，不得晋升为正常 output.mp4。

### P0-08：定义外发数据、授权、留存和敏感文档的信任边界

**问题**

“本地优先”容易让使用者误以为文档不离开本机，但默认 edge-tts 会发送完整旁白到在线服务，FAL 会接收图像 Prompt，LLM 失败又可能自动切云端。文档没有隐私模式、数据分类、用户授权、第三方留存、日志脱敏或工作区保留策略。FAL 官方当前说明 JSON 输入/输出默认保存 30 天，生成媒体生命周期还需另行配置。

**依据**

- L167、L316、L328、L441–L442、L688：默认外部服务和云端降级。
- edge-tts 项目说明其使用 Microsoft Edge 在线 TTS。
- FAL Platform Headers 文档：X-Fal-Store-IO 默认保存请求 JSON 30 天；媒体生命周期和 ACL 需显式控制。

**必须修改**

- 增加 privacy_mode：offline、approved_cloud、unrestricted；本地失败不得自动越过当前模式。
- 每个 Provider 声明接收哪些字段、目的地、留存、内容用途和故障时的替代策略；manifest 记录实际外发清单但不记录密钥。
- FAL 对敏感作业显式关闭不必要的 I/O 存储，并设置媒体过期和 ACL；其他云 LLM/TTS 做同等级条款审查。
- 日志、state、错误响应和 Prompt 做密钥/PII/正文脱敏；定义工作区和缓存的访问权限、保留期和安全删除。
- 涉及未公开、个人、受监管或合同限制文档时，云端降级必须事前授权，否则进入 needs_review/failed。

### P0-09：固定真实可调用的 FAL 模型端点、契约和能力

**问题**

方案把默认图像模型写成“FAL FLUX 2 Klein”，实现标识却是 fal-ai/flux。FAL 的具体 endpoint/model_id 决定请求字段、返回结构、限制、队列行为和计费；这不是名称差异，而是 Provider 契约无法落地。继续开发会导致客户端按错误 Schema 实现、预算失真或运行时才失败。

**依据**

- L328、L408：产品名称为 FLUX 2 Klein，API 标识为 fal-ai/flux。
- FAL 当前官方 FLUX.2 Klein 4B Base 文档使用 fal-ai/flux-2/klein/4b/base 这一具体端点，并给出该端点自己的输入/输出 Schema。

**必须修改**

- 明确一期唯一默认 SKU 和精确 model_id；在版本化配置中保存 endpoint、请求/响应 Schema 快照或哈希、能力、限制、计价单位和核验日期。
- Provider 启动时执行 capability preflight；未知参数、缺少必需能力或响应 Schema 漂移时 fail closed，不得静默映射到另一个“类似 FLUX”模型。
- 为该精确端点增加低成本真实 contract smoke，覆盖提交、排队、状态查询、结果下载、媒体校验和费用记录；经批准的 fallback 必须是显式配置并在 manifest 中标明。

### P0-10：用 TTS 时间边界或强制对齐实现字幕，并修正小于 300 ms 的验收口径

**问题**

方案选用 edge-tts，却丢弃 TTS 边界，再用 faster-whisper small 对合成语音做 ASR 回读。ASR 词时间戳不是“给定原文—音频”的强制对齐，识别替换/漏字还会使字符相似度匹配漂移。与此同时，目标写成偏差小于 300 ms，验收却只抽查 10 句的平均值，无法证明逐句硬阈值。

**依据**

- L316、L372–L374：先选 edge-tts，字幕阶段再使用 ASR 回读。
- L93 要求偏差小于 300 ms；L665 仅要求抽查 10 句的平均偏差小于 300 ms，定义互相冲突。
- edge-tts 项目支持字幕/边界元数据；faster-whisper 官方能力是 ASR 词时间戳，并把更精确对齐列为外部集成。

**必须修改**

- TTSProvider 契约输出 audio、实测 duration、timing_marks 和 provider_metadata；edge-tts 优先消费其边界事件，其他 Provider 优先使用原生 speech marks。
- 无原生 marks 时接入针对既定文本的 forced aligner；faster-whisper 用于 ASR 回读、文本覆盖 QC 或无更好能力时的显式低置信度兜底，不得单独支撑小于 300 ms 的硬承诺。
- 用人工真值集定义句/词起止误差、P50、P95、最大值、覆盖率和离群比例，并统一 AC-02 与正文。若一期不实现上述测量，应删除小于 300 ms 的硬指标，改为明确的预览级目标。

### P0-11：把真实 Provider 冒烟与故障恢复设为不可跳过的发布门禁

**问题**

方案把外部调用全部 mock，唯一真实 golden E2E 默认跳过，并把真接口 E2E 写成可选。Mock 无法发现音色下线、FAL endpoint/Schema 漂移、队列和下载语义、CTranslate2 CUDA 兼容、libass/CJK 字体或实际 ffmpeg 参数错误，也无法证明远端提交与本地状态之间的崩溃窗口可恢复。

**依据**

- L644：外部调用全部 mock，真实 golden E2E 默认跳过。
- L658：真接口 E2E 为可选；L606 却以三场景全流程通过作为验收。
- 本轮不落盘执行已经复现附录 FFmpeg fade/afade 参数失败，说明仅断言命令字符串不足。

**必须修改**

- 开发 CI 可以默认跳过付费测试，但每个发布候选必须通过不可跳过的最小真实 smoke gate：Ollama、edge-tts 目标音色、精确 FAL endpoint/队列/下载、ffmpeg/libass/CJK 字体、faster-whisper GPU 或声明的 CPU 路径，以及 30–60 秒完整成片。
- 云端调用前先原子持久化 request intent、client_request_uuid 和费用预留；Provider 支持幂等键时必须发送。拿到 request_id 后立即持久化状态，resume 对已有 request_id 只能查询/取回，不得再次提交。
- 必须承认“服务端已接受、客户端尚未保存 request_id”的未知提交窗口：若 Provider 无幂等/按客户端 UUID 查询能力，语义只能是 at-least-once。对此定义 unknown_submit 状态、远端核对、重复结果检测、人工/自动处置、单 Job/单日费用上限和孤儿请求清理，不能承诺绝对零重复计费。
- 故障注入覆盖每阶段写前/写中/写后终止、manifest 已提交但 state 未更新、磁盘满、权限错误、JSON 截断、哈希不符、429/5xx、未知提交、双 resume 和缓存损坏；验收应证明既有 request_id 不重提、未知窗口受控、截断产物不被接受且不产生伪成功。

## 6. P1：建议修改

### P1-01：补全 P0 ingest 和复杂文档解析策略

**依据**

- L153 只用一行描述 P0，正文模块设计从 P1 开始。
- L218–L232 主要依赖字号/加粗启发式和“无文本层”判断。
- PyMuPDF 官方说明 PDF 内部文本顺序可能不等于自然阅读顺序，多栏即使 sort=True 也未必正确。
- L42、L111 支持 PDF/DOCX/MD/TXT 四类输入，但 L86、L602、L664 只验收三类；OCR 在设计、依赖和里程碑间也不一致。

**建议**

- P0 定义 manifest Schema、magic/MIME、文件稳定窗口、页数/解压量/像素/内存/时间限制、复制和哈希提交。
- PDF 使用 block/span 坐标、TOC 优先、阅读顺序、页眉页脚去重、表格区域、旋转文本和逐页 OCR 判定；混合文本/OCR PDF 不能只做整文件二分。
- DOCX 补充自定义标题样式、outline level、编号列表、页眉页脚、文本框、图表/SmartArt 和不支持元素的明确报告。
- 若 OCR 属于一期，安装 rapidocr 相关依赖、模型和验收样例；否则明确一期“检测后拒绝”，不要同时宣称 OCR 降级。
- 建立文本 PDF、多栏 PDF、混合 OCR、扫描 PDF、加密 PDF、DOCX、MD、TXT、空/畸形/超限文件矩阵。

### P1-02：增加“原文视觉优先”的素材选择阶段

**依据**

- L203–L208、L491：P1 已提取表格和图片。
- L282–L335：P4/P5 只生成 visual_desc 和 AI 图像，提取素材没有消费者。
- 技术报告的图表、架构图和数字不适合由生成式插画重新创造。

**建议**

- 为场景定义 visual_source：extracted_image、page_crop、rendered_table、template_chart、generated、placeholder。
- 优先复用原文图表和页裁切；对表格/数字用确定性 HTML/SVG/Canvas 模板渲染；只有概念性配图再走 FLUX。
- 保存 source_block/page/bbox、裁切参数、版权/来源和最终资产哈希。
- 对含文字的画面由合成器确定性叠字，不要同时要求“展示报告标题”和“禁止画面文字”。L294–L295、L304、L511–L512 当前自相矛盾。

### P1-03：完善 FAL 队列、缓存和成本模型

**依据**

- L331 只定义 sha1(image_prompt + style + aspect + seed)：没有规定 style/negative prompt 的规范化内容，也明确未包含 Provider、精确模型/修订、尺寸、steps、guidance、输出格式、安全配置和后处理版本。
- L479–L490 只展示 Job 内 assets 路径，没有定义新运行是否复用既有 Job 或跨 Job 索引；因此 L671“同输入二次运行云调用为 0”目前无法由设计证明。
- FAL 官方推荐持久队列并返回 request_id；服务端默认还可能自动重试，客户端简单再调用会造成重复请求和费用。

**建议**

- 缓存 descriptor 使用 canonical JSON + SHA-256，并包含全部影响输出的参数；生成前持久化实际 seed。
- 使用明确的全局内容寻址缓存；Job 内保存引用/链接/副本和 meta，包括内容哈希、MIME、尺寸、request_id、模型、价格和许可证。若坚持 Job-local 缓存，则必须重写 AC-08，明确“二次运行”如何复用同一 Job。
- 持久化队列状态、轮询 deadline、下载校验和取消/孤儿请求处理；服务端自动重试也计入 attempt 与成本审计。
- 增加 max_cloud_calls_per_job、max_cost_per_job/day、场景/重试预算和运行前成本预估。

### P1-04：统一实测时长、最大时长和音频混合规则

**依据**

- L274 的 60 字按 4.5 字/秒实际约 13.3 秒，与“15–40 秒”不完全一致。
- L278 只按估算让 LLM 压缩，L321 才得到实测音频时长；没有实测总长超过 max_duration 后的确定性策略。
- L343 的留白会使视频总长大于音频总和，L387 却按音频总和做 ±5% 检查。
- L346、L773 先归一旁白再与 BGM 混合，最终成片不再保证目标响度；固定 volume=0.12 也不能保证语音可懂度。

**建议**

- P5 后按实测音频和 lead/trail 重新计算全片时长；超限时确定性地重写/删减低优先级场景，再重新 TTS，而不是任意 atempo。
- 统一时间轴公式并让字幕、场景视频、concat 和 QC 使用同一 render_timeline。
- BGM 使用旁白侧链 ducking；先完成最终混音，再执行两遍 loudnorm 或等价可复现流程，并检查最终 LUFS、true peak、削波、声道和采样率。
- QC 时长基准应是 render_timeline 的预计总长，不是纯音频时长总和。

### P1-05：按错误类型、deadline 和远端提交语义重写重试策略

**依据**

- L575–L583 只给次数和固定退避，没有区分鉴权、配额、审核拒绝、参数/Schema 错误、429、5xx、超时和取消。
- 没有 Retry-After、jitter、单调用/单场景/整 Job deadline、熔断或重试总预算。

**建议**

- 分类 retryable、terminal、auth、quota、moderation、timeout、cancelled；401/403、非法参数、Schema 不兼容不得盲目重试。
- 对 429/5xx/网络错误使用带 jitter 的退避并尊重 Retry-After；保存 attempt history，而不是只有计数。
- 远端调用前原子记录 intent、client_request_uuid 和预算；有幂等键时使用。已持久化 request_id 的请求恢复时只查询旧请求；无幂等能力的未知提交窗口按 at-least-once 语义进入核对/去重/费用封顶流程。服务端自动重试计入成本和总体 attempt 上限。
- Ctrl+C、进程退出、超时和取消后要核对远端孤儿请求并保存可恢复状态。

### P1-06：重新定义 QC 的硬失败、警告和量化指标

**依据**

- L388、L675 允许一个超过 2 秒的静音段，可能让整段无声通过。
- L389、L675 用平均亮度小于 16 判断黑帧，但 L302 支持 tech-dark 深色风格。
- L390 以字幕条数/场景数检查，L675 又以字幕总时长 90% 检查，定义不一致。
- 没有最终 loudness、true peak、编码 profile/pix_fmt、字体缺字、字幕文本覆盖、占位率或全片解码检查。

**建议**

- 定义 pass/warn/fail 和发布门禁矩阵；audio_failed、chapter_skipped、文件不可完整解码、字幕缺失、编码规格不符为 hard fail。
- 对每场景检查语音能量覆盖、最长异常静音和总静音比例；黑帧使用时间连续性/blackdetect 并按风格校准。
- 字幕检查规范化原文覆盖率、时间交叠、越界、P95/max 误差和字体渲染。
- 对最终 MP4 做全量 decode-to-null，再用 ffprobe 检查 H.264 profile、pix_fmt、分辨率、fps、AAC、采样率、声道、faststart。
- 对 placeholder、重复图、图文相关性和生成图乱码文字设上限或 needs_review。

### P1-07：补全不可信输入、watch 和子进程安全

**依据**

- L111 允许 200 MB 文档，L573 watch 自动入队；仅文件大小不能防 ZIP bomb、超页数、超像素或畸形文档。
- L561、L764–L781 允许外部路径进入 concat、ASS 和滤镜流程，却没有路径 containment 或参数转义规则。
- “正在处理的文件改名跳过”不能可靠判断生产者是否仍在写文件。

**建议**

- 明确输入信任模型。若 watch 可能接收非完全可信文件，P0/P1 加入 quarantine、资源限额、超时、路径和符号链接防护。
- watch 不修改源文件；等待 size/mtime 稳定且可打开，复制到 Job staging，验证 magic 和哈希后原子入队。
- DOCX 限制解压后总量/条目数，图片限制像素，PDF 限制页数/对象和解析时间。
- 子进程使用参数数组和 shell=False；相对路径规范化后验证仍位于 Job 根。对空格、Unicode、引号、换行、盘符、长路径建立安全测试。

### P1-08：落实环境和依赖可复现性

**依据**

- L425、L438、L689 声称锁定 ffmpeg 6.x，L630 却用无版本约束的 winget install；本机实际已安装 8.1.2。
- L625–L627 使用 Bash 反斜线续行，但目标系统是 Windows 11，PowerShell 不能直接照抄。
- L637 建议用 python-dotenv 读 .env，依赖列表却没有 python-dotenv。
- L780 引用 assets/fonts，但目录结构没有 fonts；也未预检 libass 和 CJK 字体。

**建议**

- 提交 uv.lock，固定 Python 依赖、模型 digest、Prompt 版本、FAL endpoint 和 ffmpeg build/功能集合。
- 提供 PowerShell 可直接执行的安装命令；补充 python-dotenv 或明确只读取系统环境变量。
- 增加 doctor 命令，检查 ffmpeg filters/encoders、字体、Ollama 版本/模型/上下文、FAL/edge-tts 连接、CTranslate2 GPU/CPU 和可用磁盘。
- 不必强行锁死旧 6.x；更稳妥的是固定已验证 build，并以功能探测和回归矩阵支持明确版本范围。

### P1-09：重估里程碑、关键路径和工期

**依据**

- L601–L606 的区间相加为 12–20 人日，即 2.4–4 个五日工作周；L608 却写单人 2–3 周。
- M2 产出讲稿，M3 才能得到真实素材和音频时长，M4 才能完成字幕与合成，主集成路径并不能简单并行。
- M2–M5 没有独立列出 P4 分镜、契约冻结、安全/隐私、故障恢复、真实 Provider 发布测试和运维硬化。
- L610 所称“每任务 2–5 分钟”与实际小时/天级工程任务明显不符。

**建议**

- 区分三个交付级别：
  - Happy-path PoC：2–3 周，只保留有限输入、单 Provider、无 watch/OCR/强恢复承诺。
  - 功能完整 MVP：4–6 周，覆盖四类输入、真实 Provider、基本断点和量化验收。
  - 无人值守发布级：建议至少 6–8 周，并另留 20%–30% 风险缓冲，覆盖安全、隐私、故障注入、成本和运维。
- 子代理可并行 Provider 桩、测试和文档，但必须保留 M2 → M3 → M4 的真实集成窗口；1–1.5 周不能作为发布级承诺。
- 任务拆为 0.5–2 人日工作包，每项有输入、产物、测试和退出条件；新增“契约/技术 Spike”“安全合规”“发布硬化”里程碑。

### P1-10：扩充风险清单并加入可执行触发器

**依据**

L681–L691 只有 R1–R9，缺少多类高概率且高影响风险。

**建议至少增加**

- 隐私、数据跨境、供应商留存和云端授权。
- 密钥泄露、日志泄密、最小权限和跨项目密钥复用。
- Prompt injection、恶意 PDF/DOCX、ZIP bomb、路径/命令注入和资源耗尽。
- Provider API、模型端点、音色、返回 Schema 和服务条款漂移。
- 远端重复请求、服务端/客户端双重重试、费用与额度耗尽。
- 磁盘满、缓存损坏、状态截断、临时文件和工作区生命周期。
- GPU OOM、CPU/RAM 争用、批处理雪崩和意外 CPU offload。
- 输入/BGM/字体/生成图的版权、商标、肖像、声音及许可证。
- 运行时依赖许可证与分发义务：PyMuPDF 为 AGPL/商业双许可；edge-tts 项目许可证为 LGPL-3.0（具体文件例外以仓库许可证为准）；本机 FFmpeg build 含 --enable-gpl 和 libx264，FFmpeg 官方说明启用 GPL 组件会使该 build 适用 GPL。若分发闭源客户端或打包二进制，这一项必须成为发布阻断条件；上线前按部署/分发模式完成许可证选择、源码/NOTICE/构建参数义务和法务复核。
- LLM 幻觉、内容遗漏、敏感领域错误和公开发布审批。
- 依赖供应链、CVE、ffmpeg build 差异、CUDA/cuDNN 兼容。
- 进程取消、系统重启、远端孤儿请求、Windows Unicode/长路径和字体缺字。

每项不只写“影响/对策”，还应记录概率、严重度、责任人、检测信号、触发阈值、预防、恢复和残余风险。

### P1-11：补充磁盘、缓存、日志和保留期运维设计

**依据**

- L111 允许 200 MB 输入；L479–L495 每个 Job 复制原文并产生图片、音频、中间片段和成片。
- L565、L570–L573 支持长期 batch/watch。
- L101 又要求不丢素材，但没有配额、清理或失败临时文件策略。

**建议**

- 运行前估算并检查磁盘，设置 Job/全局缓存配额、最大工作区数、LRU/TTL 和失败产物保留期。
- 缓存命中必须校验长度、SHA-256、MIME、分辨率/时长和可解码性；不能只看文件存在。
- 提供 cache stats、gc、archive 和“只保留最终产物+审计 manifest”模式。
- 结构化日志包含 job/phase/scene/request_id，但正文、Prompt、密钥和 PII 默认脱敏；配置轮转和最大容量。

### P1-12：把“超长自动分集”从口号变成契约和父子状态

**依据**

- L65、L686 声称超长文档自动分集。
- L278 实际只让 LLM 把单片压缩到 600 秒。
- 当前目录、Schema、CLI、状态和交付都没有 episodes、父 Job、子 Job 或多成片定义。

**建议**

- 定义分集阈值、章节不可拆/可拆规则、跨集重复引言/结尾、总集数上限和每集成本/时长。
- manifest 增加 episodes，父 Job 汇总子 Job 状态；每集独立恢复、QC 和产物路径。
- 若一期不实现，应从 A4、R4 和验收中删除“自动分集”，明确超限拒绝或人工拆分。

## 7. P2：可选优化

### P2-01：以 Pydantic 为单一契约来源并生成 Schema

技术栈已同时包含 Pydantic 和 jsonschema（L427）。可从版本化 Pydantic 模型生成提交的 JSON Schema，并由 CI 检查生成结果未漂移；另保留跨文件语义验证器。这样可减少 Python 类型、序列化和外部契约的双份维护。

### P2-02：增加 append-only 事件日志

在原子 state.json 快照之外增加 events.jsonl，记录状态转移、attempt、失效原因、人工覆盖和 Provider 请求。它不是替代锁和原子提交，而是用于审计和在快照损坏时重建。

### P2-03：增强跨场景视觉一致性

风格前缀本身不能保证人物、配色和构图一致。可固定模型/seed 策略、使用参考图或可控编辑端点、生成全局 style bible、对色板做后处理，并用相似度/重复度检查。对于标题、数字和品牌元素，继续使用确定性模板叠加。

### P2-04：补充中文字幕可用性与无障碍

增加 CJK 字体打包、每行字数、最多两行、最短显示时长、标点断句、中英混排、数字/专名发音词典、对比度、横竖屏安全区和平台 UI 避让。Scene 不应被定义成“一段字幕”；一个场景通常包含多条 cue。

### P2-05：按冷/热缓存和分位数定义性能 SLO

把 AC-07 拆成冷缓存/热缓存、短文/5000 字/30 页、横屏/竖屏的 P50/P95；分别记录本地计算、云排队、生成、下载和总等待。默认路径依赖云队列时，不能排除排队后仍称为端到端 SLA。

### P2-06：当前无需为了“看起来完整”引入重型编排平台

在单机线性主链和少量 fan-out 场景下，asyncio 可以保留。先把产物、状态、锁、远端 request_id 和事件日志做正确；只有出现多机、优先级队列、长期调度、可视化运维或多租户需求时，再评估 Prefect、Temporal 等平台。

### P2-07：统一编号、术语和示例，消除次要歧义

L312、L324 在 4.5 节内误写为“5.5.1/5.5.2”；L490 同时示意 images/sc01.png 并称文件名等于缓存键，而 L516 又使用哈希名；L73 把 Scene 定义为“一段字幕”，但实际一个场景通常会产生多条 cue；L65 声称默认视频 1–10 分钟，而 L719 的官方示例约 37 秒。应统一编号、缓存对象与场景链接的命名、Scene/Cue 关系，并说明 1 分钟是目标区间还是硬下限。

### P2-08：兑现“零上下文可开发”的文档自包含承诺

L6、L792 把本文档定义为零上下文实现依据，但 L756 省略了摘要 Prompt，L642 又引用仓库中不存在的外部 TDD 技能。除真实 Schema 外，还应把 Prompt、Provider 请求/响应 fixture、配置默认值、错误码、测试数据授权和完成定义全部版本化纳入仓库；否则应降低文档定位为“架构草案”，不能称作唯一实现规范。

## 8. 建议的最小修订后产物流

在保持 8 阶段前提下，建议：

1. P0：manifest.json + 不可变 input snapshot。
2. P1：parsed.json + extracted assets manifest。
3. P2：grounded_summary.json，包含逐章摘要和源证据。
4. P3：script.json，每场景引用源 block/page。
5. P4：scene_plan.json，不含下游素材结果。
6. P5：assets_manifest.json + 二进制资产 + TTS timing marks，按场景逐项 checkpoint。
7. P6a：subtitles.json/SRT/ASS；P6b：render_manifest.json + staging MP4。
8. P7：qc_report.json；硬门禁通过后原子发布 final/output.mp4。

state.json 只引用阶段产物、Schema 版本、内容哈希、stage fingerprint 和状态，不承载或覆写业务数据。所有阶段产物不可由下游原地修改。

## 9. 建议的复审准入条件

以下条件全部满足后再提交复审：

- 全文阶段编号、依赖图、目录、CLI、state 和验收一致；字幕先于烧录，QC 是发布门禁。
- 提交所有真实 Draft 2020-12 Schema、示例实例和跨文件语义校验规则。
- P4/P5 产物分离；state 具备原子提交、完整输出复核、配置/模型指纹、作业锁和逐场景 checkpoint。
- 讲稿每个事实可追溯到源 block/page，且关键数字/专名有自动一致性检查。
- FFmpeg 横/竖屏命令在固定 build 上真实执行通过，并由 ffprobe/全量解码验证交付规格。
- 默认 Provider 各完成一次真实 smoke；FAL 使用已冻结的精确 endpoint/Schema，字幕以 timing marks/forced alignment 通过统一误差指标。故障注入证明已有 request_id 的恢复不重提，unknown_submit 按 at-least-once 策略完成核对、去重和费用封顶；截断缓存不得被接受，也不得产生伪成功。
- privacy_mode、外发清单、FAL 留存/ACL、日志脱敏和密钥策略可执行。
- 工期按 PoC、MVP、发布级重新估算，并为未验证性能给出基准计划和 P50/P95。

## 10. 官方核验资料

- [PyMuPDF FAQ：文本阅读顺序、多栏限制与双许可证说明](https://pymupdf.readthedocs.io/en/latest/faq/index.html)
- [Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [Ollama Thinking](https://docs.ollama.com/capabilities/thinking)
- [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
- [Ollama Context length：不同显存档位的默认上下文](https://docs.ollama.com/context-length)
- [Ollama FAQ：上下文、并发与内存](https://docs.ollama.com/faq)
- [edge-tts 项目说明与字幕能力](https://github.com/rany2/edge-tts)
- [edge-tts 项目许可证](https://github.com/rany2/edge-tts/blob/master/LICENSE)
- [FAL FLUX.2 Klein 4B Base API](https://fal.ai/models/fal-ai/flux-2/klein/4b/base/api)
- [FAL 异步队列与 request_id](https://fal.ai/docs/documentation/model-apis/inference/queue)
- [FAL Platform Headers：留存、媒体生命周期与重试](https://fal.ai/docs/documentation/model-apis/common-parameters)
- [faster-whisper 官方仓库：word timestamps、GPU 要求与外部对齐集成](https://github.com/SYSTRAN/faster-whisper)
- [FFmpeg Filters：fade、afade、apad、loudnorm](https://ffmpeg.org/ffmpeg-filters.html)
- [FFmpeg concat demuxer 约束](https://ffmpeg.org/ffmpeg-formats.html#concat)
- [FFmpeg Legal：构建选项与 GPL/LGPL 适用边界](https://ffmpeg.org/legal.html)
- [JSON Schema 官方入门](https://json-schema.org/learn/getting-started-step-by-step)

---

最终结论再次确认：**不通过。** 这是对当前 v0.1“可直接作为唯一开发依据”的否定，不是对总体产品方向的否定。完成全部 P0、落实复审准入条件后，应提交 v0.2 重新评审。
