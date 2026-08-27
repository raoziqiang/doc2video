# 《技术方案.md》v0.2 技术复审报告

## 1. 复审信息与证据边界

- **复审对象：** 仓库根目录《技术方案.md》，版本 v0.2，共 913 行。
- **对照基线：** `review_report.md` 中 v0.1 的 P0-01～P0-11、P1-01～P1-12，以及 P2-01～P2-08。
- **复审日期：** 2026-08-27。
- **复审范围：** 原意见逐项关闭情况、v0.2 新增设计的一致性、阶段/状态/字段/编号一致性、可执行性，以及最终复审结论。
- **仓库现状：** 本轮只读枚举确认，仓库业务文件仍只有 `技术方案.md` 与 `review_report.md`。文中规划的 `contracts/`、`schemas/`、`config/`、`prompts/`、`fixtures/`、`uv.lock`、CI、测试和实现代码均不存在。因此，本报告区分：
  - **“文本已落实”**：v0.2 已把要求写成足够清楚且内部一致的设计规范；
  - **“计划落实”**：文档只把事项放入 M0/M1 等未来工作；
  - **“已验证”**：存在当前仓库产物或本轮可重复的只读核验依据。
- **只读约束：** 除新增本报告外，没有修改任何其他文件；没有调用付费 Provider、没有生成媒体、没有改动本机配置。FFmpeg 仅执行了帮助信息查询，未落盘。
- **外部契约核对：** 仅针对本轮新增的可变 Provider/工具主张复核了官方公开资料。FAL 当前同时存在 `fal-ai/flux-2/klein/4b` 与 `fal-ai/flux-2/klein/4b/base` 等不同契约端点，进一步说明必须选定唯一 SKU，而不能只写产品族名称；FFmpeg 官方定义 `sidechaincompress` 的第一个输入为被压缩的 main、第二个输入为 sidechain；OpenAI 当前公开的 Create speech API/官方 Python SDK 未把 word/sentence speech marks 定义为标准返回契约。

## 2. 最终结论

**复审结论：不通过。**

这个结论针对的是 v0.2 当前第 5 行所称“全部 P0/P1 已吸收”、以及第 7、913 行所暗示的“可与仓库契约共同构成开发依据”。它不是对总体架构方向的否定。

v0.2 已完成几项关键纠偏：P6 字幕先于 P7 合成、P8 成为交付门禁；讲稿引入源块引用和 facts；核心失败改为 fail closed；字幕以 TTS marks 为主；真实 Provider smoke 被写成发布候选不可跳过；自动分集被明确延期；工期与风险范围也比 v0.1 现实得多。上述改动足以说明方案已从“不一致的概念稿”进步到“可继续做 M0 风险消减的架构草案”。

但当前仍有三个无争议的直接阻断项：

1. **P0-03 未关闭：** 只有一份内嵌 `scene_plan` Schema，其余核心 Schema/Pydantic 模型未提交；而且文内 P4 示例本身不满足该 Schema 的 required 字段。
2. **P0-06 未关闭：** 附录 C.3 的 `sidechaincompress` 输入接反，实际压缩旁白而不是 BGM，随后又把两路旁白混合；正文承诺两遍 loudnorm，示例却只有一遍。
3. **P0-09 未关闭：** 多处写“精确端点冻结”，选型表却明确写“待 M0 Spike 冻结”，并保留 FAL 直连与 Hermes 网关两个不同候选，当前没有唯一 model_id、契约快照、计费与核验结果。

此外，P0-02、P0-04、P0-05、P0-08 和 P0-10 均仍有会影响可实现性或发布安全的闭环缺口。故 v0.2 可以进入 M0 Spike，但不能作为已通过复审的冻结实现规范，也不能进入“按本文直接并行开发全部模块”的状态。

## 3. P0-01～P0-11 逐项核对

### 3.1 汇总表

| 原意见 | 复审判定 | v0.2 已吸收内容 | 尚未关闭的关键点 |
|---|---|---|---|
| P0-01 阶段依赖与编号 | **已落实** | P6 字幕 → P7 合成 → P8 QC/发布；P0～P9 已同步到图、表、目录和 CLI；`--no-burn-subs` 只跳烧录 | `render_timeline` 的产物归属另计入 P0-04/P0-06 |
| P0-02 可追溯讲稿 | **部分落实** | block/page/bbox/reading_order/OCR confidence、chapter summaries、facts、coverage、scene source IDs、回取原文均已写入 | 稳定 block_id 算法、claim/fact 级绑定、单位/否定关系验收和 coverage 算法未闭合 |
| P0-03 真 Schema 与不可变产物 | **落实不到位，阻断** | 给出一份真正 Draft 2020-12 形状的 Schema；P4/P5 产物分离原则正确 | 其余 Schema/模型不存在；唯一示例不通过唯一 Schema；条件约束、迁移、artifact manifest 契约缺失 |
| P0-04 崩溃/并发安全状态机 | **部分落实，阻断** | 状态名、指纹、原子替换、锁、checkpoint、events、unknown-submit 原则已写 | 状态转移不完整；`unknown_submit` 不在枚举；resume 全量复核算法和 artifact manifest 契约缺失 |
| P0-05 tokenizer 与资源调度 | **部分落实** | 改为 tokenizer 计数、NUM_PARALLEL=1 起步、GPU 互斥、M0 基准 | tokenizer/revision 未冻结；缺安全余量及多项 LLM 配置；NUM_PARALLEL=1 仍只是注释而非可证明 preflight |
| P0-06 FFmpeg 时间轴/命令 | **部分落实，阻断** | 纯数值时间、横竖屏参数化、统一流参数、参数数组、真实 fixture 测试原则已写 | C.3 sidechain 硬错；两遍 loudnorm 与示例冲突；timeline 归属、fade 数值、level/timebase 尚不唯一 |
| P0-07 核心失败关停 | **已落实（设计层）** | 不跳章、正式 TTS 不静音、占位受限、字幕/事实/音频失败关停、P8 不过不发布 | 仍应补完整发布矩阵和 preview 隔离，但不再构成该原 P0 的主缺口 |
| P0-08 隐私/授权/留存 | **部分落实，阻断** | 三档 privacy_mode、脱敏、留存/ACL 原则和敏感文档授权已写 | 没有真实 Provider 信任矩阵、数据分类和授权凭证；默认外发与“默认保守”矛盾；P0 manifest 无法记录后续实际外发 |
| P0-09 FAL 唯一端点 | **未落实，阻断** | 已把冻结动作和 contract smoke 放入 M0/发布门禁 | 当前仍是两个候选且无唯一 model_id/Schema/hash/价格/核验日期；“已冻结”与“待冻结”直接矛盾 |
| P0-10 字幕对齐口径 | **基本落实，但需修正契约** | marks → forced aligner → ASR 低置信兜底；真值集 P50/P95/max/覆盖率已写 | no-marks Provider 与强制 `timing_marks` 输出冲突；OpenAI TTS marks 能力误标；aligner 未选型；低置信兜底状态未定义 |
| P0-11 真实 smoke/恢复门禁 | **已落实（制度设计），执行未验证** | 发布候选不可跳过；真实 Ollama/TTS/FAL/ffmpeg/whisper/短 E2E、故障注入与 request_id 恢复均已列入 | 当前无 CI/结果；FAL 目标未冻结；unknown-submit 未进入正式请求状态机；门禁如何防手工跳过未定义 |

### 3.2 P0-01：字幕、合成、QC 的依赖和编号

**判定：已落实。**

证据：

- 架构图明确 P5 → P6 字幕、P5/P6 → P7 合成、P7 → P8 QC/发布（L137～156）。
- 阶段表统一为 P0～P9（L158～171）；详细模块也依次为 4.7 P6、4.8 P7、4.9 P8、4.10 P9（L371～433）。
- `--no-burn-subs` 在输出规格、合成步骤和 CLI 中均明确为“只跳过烧录”（L120、L398、L622）。
- P7 只写 staging，P8 通过后才生成 final（L400、L406～423）。

保留意见：P8 的“final 目录先写 staging 名，门禁通过后 `os.replace` 晋升”（L421）应改成精确的同文件系统单文件提交协议，并说明 P7 `render_manifest` 在 staging 文件被移动后如何仍通过 resume 复核。此问题属于新的提交/产物归属问题，不否定 P0-01 的阶段顺序已经修正。

### 3.3 P0-02：讲稿事实保真与源文档追溯

**判定：部分落实。**

已经落实：

- P1 block 示例有 `block_id/page/bbox/reading_order/ocr_confidence`（L218～230）。
- P2 增加逐章摘要、facts、source IDs/pages 和 coverage（L250～268）。
- P3 每场景保存 source IDs/pages，并要求回取相关 parsed 原文块，禁止只从总摘要扩写（L273～300；Prompt 见 L838～855）。
- P8/AC-04 增加事实与章节覆盖门禁（L418、L776）。

仍未闭合：

1. `block_id` 只有 `b1` 形式的示例，没有稳定构造规则、规范化、碰撞处理以及解析器升级后 ID 是否保持的定义。原意见要求的是“稳定 block_id”，不是任意序号。
2. `key_points` 仍写成字符串数组 `"要点(附 source_block_ids)"`（L254），本身没有结构化 source IDs。
3. facts 枚举包含 `unit|negation`（L258），但 P3 的确定性检查只列数字/日期/专名（L294），AC-04 又只点名数字/专名（L776）。单位、否定关系没有进入验收闭环。
4. 当前只有 scene → block 的粗粒度引用；没有 `fact_id`、规范化值/单位/极性，也没有 scene claim → fact → source block 的绑定。`source_block_ids` 非空或属于 parsed 集合，只能证明引用存在，不能证明引用支持该句主张。
5. coverage 只有 seen/total/uncovered 计数，没有说明页眉页脚、目录、低信息块是否必须覆盖，也没有章节/关键事实覆盖率的计算规则。P8 所称“证据不足”因此还不可执行。
6. 高风险领域人工复核只出现在 R19（L810），缺领域判定、审批记录、解除 `needs_review` 的流程。

### 3.4 P0-03：真实 Schema、版本化契约和不可变产物

**判定：落实不到位，P0 阻断。**

正确改动：

- Pydantic 单一来源、Draft 2020-12、CI 防漂移、`additionalProperties:false` 等方向正确（L175、L452、L539～587）。
- P4 写 `scene_plan.json`、P5 写 `assets_manifest.json`，不再覆写上游产物（L304、L338～340、L521～533）。

阻断问题：

1. 原意见要求 manifest、state、parsed、summary、script、scene plan、assets manifest、subtitles、render manifest、QC report 共十类 Schema；仓库当前没有 `contracts/` 或 `schemas/`，文内也只给出一个 `scene_plan` Schema。其余九类以及新增的 per-stage `artifact_manifest` 契约均缺失。
2. 文档自己的 P4 示例无法通过自己的 Schema：
   - P4 示例的 scene 只有 `id/visual_desc/visual_source/image_prompt/aspect/extracted_ref`（L306～318）；
   - Schema 却把 `chapter/narration/est_duration_s/source_block_ids` 列为 required（L567～579）。
3. `aspect`、`image_prompt`、`extracted_ref`、`source_pages` 未被 required，也没有 `if/then` 条件。例如 `generated` 可没有 prompt，`page_crop` 可没有 bbox/ref，Schema 会接受不可合成或不可追溯的实例。
4. L589 写“引用集合完全匹配”，括号中却只给 `source_block_ids ⊆ parsed.blocks`；子集合法不等于 script scenes、scene plan、assets、字幕 cues、render segments 的集合一一匹配。
5. 没有 Schema 迁移/向后兼容策略，旧 Job 在 Schema/模型升级后如何 resume 未定义。

### 3.5 P0-04：崩溃安全、并发安全状态机

**判定：部分落实，P0 阻断。**

已经吸收：状态名、stage fingerprint、tmp/fsync/os.replace、多文件提交点、Job/cache 锁、P5 checkpoint、events.jsonl、人工 revision 和断电安全边界均有明确文字（L591～607）。远端 intent/request_id/unknown-submit 原则也已加入（L661～668）。

仍未闭合：

1. 状态图只有 `pending → running → committing → ...` 和 `succeeded → invalidated → pending`（L595～598），没有：
   - `failed/needs_review/cancelled` 的重试或人工批准转移；
   - `succeeded_with_warnings` 的失效/重跑转移；
   - running 阶段直接失败/取消的合法边；
   - committing 崩溃后的正式转移。
2. L600 规定“上游未 succeeded 时下游不得运行”，字面上排除了 `succeeded_with_warnings`，使该状态无法作为非阻断成功状态使用。
3. L665 定义 `unknown_submit` 状态，但它不在 L595～598 的阶段状态，也没有独立 request checkpoint 状态枚举/转移。
4. 缺原 P0 明确要求的 resume 算法：从 P0 起重新读取每阶段提交清单，核实实际 SHA-256、长度、Schema、语义、媒体探测、fingerprint，从最早 dirty 节点级联 invalidation。当前只写“任一指纹变化则失效”，没有说明成功文件丢失、替换、截断或语义损坏如何被发现。
5. `artifact_manifest` 没有固定文件名、Schema、revision、产物路径/哈希字段以及与 state 的引用关系。P1/P5/P6/P7 等多文件阶段无法仅凭自然语言实现唯一提交点。
6. Windows 跨进程锁的实现、stale lock 回收、revision CAS 和锁粒度未定义。
7. `events.jsonl` 被赋予“快照损坏重建”职责（L606），但没有尾部半行、并发追加、事件序号/校验、重复事件和 fsync 规则。

### 3.6 P0-05：token 预算与 GPU/并发资源

**判定：部分落实。**

已经吸收：改用目标模型 tokenizer 计数并递归细分（L265～268）；12 GB 主机从 NUM_PARALLEL=1 起步（L481～484）；Ollama/whisper/ComfyUI 受全局 GPU 调度（L488）；M0 有基准任务（L712）。

仍未闭合：

1. 没有冻结 tokenizer 的实现来源、revision/digest，也没有说明其与 Ollama 实际模型模板/特殊 token 是否一致；“实测 tokenizer”仍不可直接实现。
2. L267 的预算公式没有明确安全余量；全文也没有冻结 `max_output_tokens`、model digest、量化、temperature、seed、结构化输出模式等原要求字段。
3. L484 写“num_ctx 固定（建议 8192）”，“固定”与“建议”不是同一契约。
4. L483 要求 NUM_PARALLEL=1 起步，L737 又写“先不改”；当前没有 doctor 在服务端并行值未知或不是 1 时 fail closed 的规则。应用的 `--jobs 1` 不能证明 Ollama 服务端并行配置。
5. OOM、CPU offload、503 只写“明确定义降级或失败”（L488），尚未给错误 → 状态/退出码/SLO 标记的实际映射。

### 3.7 P0-06：FFmpeg 时间轴与命令模板

**判定：部分落实，P0 阻断。**

已经吸收：Python 预计算纯数值、参数数组与 `shell=False`、横竖屏 scale/crop/pad、concat 前统一流参数、High/yuv420p/48 kHz、ffprobe/decode-to-null 和真实 fixture 测试原则均已进入正文（L387～417、L748～750、L870～902）。

新增或遗留硬伤：

1. **C.3 sidechain 输入接反。** L890～893 使用 `[0:a][1:a]sidechaincompress[bgm]`，而本机 `ffmpeg -h filter=sidechaincompress` 与 [FFmpeg 官方文档](https://ffmpeg.org/ffmpeg-filters.html#sidechaincompress)均表明输入 #0 是被压缩的 main，输入 #1 才是 sidechain。当前图会让 BGM 触发压缩旁白，然后把原旁白与被压缩旁白混合，真正的 BGM 没进入最终混音。应让 BGM 作 main、旁白作 sidechain，再把 ducked BGM 与原旁白混合。
2. 正文 L397 承诺两遍 loudnorm，C.3 只有一次 `loudnorm=I=...`（L892），没有第一遍测量和第二遍 `measured_*` 参数；文档内部冲突。
3. L872 举例 `FADE_OUT_ST=12.4`，同一个 12.7 秒 C.1 示例却写 `st=12.1`（L878～880）；fade 应在旁白结束、trail 开始还是成片结束时完成没有唯一公式。
4. 原要求中的兼容 `-level:v` 仍缺；“统一 timebase”只在正文宣告，示例没有冻结 timebase；最终 C.4 也未再次约束 `-ac 2`。
5. `render_timeline` 被称为 P6/P7/P8 的单一事实源（L389），但目录只在 P7 `render_manifest.json` 中隐含 timeline（L529）。P6 先于 P7，无法引用尚未提交的 P7 产物。应把 timeline 变为 P5 后、P6 前生成的独立不可变产物，或明确由哪一阶段提交并供三阶段引用。
6. 竖屏只有文字替换说明（L883），没有一条冻结的 9:16 参数数组/fixture；这可留作 M0 测试，但不能声称模板已完全统一。

### 3.8 P0-07：核心内容/口播失败关停和发布门禁

**判定：已落实（设计层）。**

- NFR 和设计原则明确章节、旁白、字幕、事实覆盖缺失必须失败（L102、L178）。
- 正式 TTS 失败不得静音；静音只允许 preview（L350～353、L655～658）。
- 图像占位受最大 10% 和连续 2 个限制（L364）。
- P8 明确 staging 不得在门禁失败时晋升（L406～423）。

尚需作为 P1 完善：

- 给出 pass/warn/fail/needs_review → 是否发布的完整矩阵；目前 `succeeded_with_warnings` 的发布语义不明确。
- preview 模式没有 CLI/配置入口、独立目录和不可发布标记，应保证静音预览永远无法被 P8 晋升。

### 3.9 P0-08：privacy_mode、授权、留存和信任边界

**判定：部分落实，P0 阻断。**

三档模式、脱敏、敏感作业授权、FAL 留存/ACL 原则均已出现（L670～682），但还没有形成可执行边界：

1. L678 只要求“每个 Provider 声明”，正文和仓库并没有 edge-tts、FAL、云 LLM/TTS 的实际信任矩阵：发送字段、接收主体/区域、留存期限、训练用途、删除能力、fallback 均未冻结。
2. A6 称“默认保守”（L64），但默认 `approved_cloud` 自动允许 FAL 与 edge-tts（L675）。对可能敏感的未知文档而言，这不是保守默认。
3. L681 只要求敏感文档的“云端降级”事前授权；FAL/edge-tts 是默认主路径，即使没有发生降级也会外发 prompt/旁白。首次默认外发也必须受同一授权或分类门禁。
4. 没有数据分类字段/流程，也没有授权 receipt（批准人、Job、Provider、字段范围、时间、期限、撤销）。系统无法执行“未公开、个人、受监管”的判断。
5. P0 的 `manifest.json` 在 ingest 时生成且不可变（L210）；L678 却要求 manifest 记录“实际外发清单”。后续真实调用发生在 P2/P5，既不能回写 P0 产物，也无法预先知道实际外发。应新增不可变 `egress_manifest.json`/审计事件，由后续阶段提交，而不是修改 P0 manifest。
6. FAL 的存储头、媒体过期/ACL 值、失败时是否 fail closed、其他 Provider 的等价控制都未具体化。
7. `offline` 依赖本地 TTS/ComfyUI，但两者均只是可选项；必须定义缺本地 Provider 时在 P0/doctor preflight 立即失败。

### 3.10 P0-09：FAL endpoint、契约和能力冻结

**判定：未落实，P0 阻断。**

- L13、L63、L359 使用“精确端点冻结”的完成时语气。
- L448 却明确写“model_id 待 M0 Spike 实测冻结”，并保留 `fal-ai/flux-2/klein/4b/base` 与 Hermes 网关对应模型两个候选。
- M0 才计划把 Spike 结论写入 config（L712）。

FAL 官方当前确有不同 SKU/契约页面，例如 [FLUX.2 Klein 4B](https://fal.ai/models/fal-ai/flux-2/klein/4b/api) 与 [FLUX.2 Klein 4B Base](https://fal.ai/models/fal-ai/flux-2/klein/4b/base/api)。二者不是可互换字符串，输入字段、步数、能力、延迟和计费可不同；Hermes 网关又是第三种客户端契约。因此 M0 Spike 是合理的风险消减动作，但“计划在 M0 冻结”不等于 v0.2 已冻结。

关闭本项至少需要：唯一默认 endpoint/model_id、请求/响应 Schema 快照或 hash、能力/限制/默认值、计价单位、核验日期、队列与取消语义、留存控制、真实低成本 contract smoke 结果，以及显式 fallback。P0-11 的 FAL smoke 在此之前也没有唯一测试目标。

### 3.11 P0-10：TTS marks、强制对齐和 `<300 ms` 指标

**判定：基本落实，但契约仍需修正后才能关闭。**

正确改动：

- marks → forced aligner → ASR 低置信兜底的优先级正确（L371～383）。
- 已明确 ASR 词时间戳不等于强制对齐，不能单独支撑硬承诺（L379）。
- AC-02 改为人工真值集上的 P50/P95/max/覆盖率/离群比；没有真值集时删除硬指标（L774）。
- edge-tts 当前实现确有 WordBoundary/SentenceBoundary metadata，可作为默认主路径。

仍需修改：

1. TTSProvider 契约强制输出 `timing_marks`（L350），FR-05 又要求每个音频都有 marks（L91）；P6 同时允许无原生 marks 的 Provider 再做 forced alignment（L378）。应将 P5 的 `native_timing_marks` 设为可空，并让 P6 产生统一、必填的 `aligned_marks/cues`，否则接口自相矛盾。
2. L347 把 OpenAI TTS 标成“原生 speech marks ✅”。截至复审时，[OpenAI Create speech 官方 API](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)和[官方 Python SDK Speech 接口](https://github.com/openai/openai-python/blob/main/src/openai/resources/audio/speech.py)没有把 word/sentence timestamps/speech marks 定义为标准返回字段；只能在 M0 contract smoke 证明后标 ✅，否则应列为“无原生 marks，走 aligner”。
3. forced aligner 只写“如 whisperX/对齐器”，未冻结依赖、模型、许可证、中文能力和失败状态；`uv add` 也没有该依赖。
4. ASR 低置信兜底若无法满足 AC-02，必须明确进入 `needs_review` 或 preview-only；不能仅写“不得支撑承诺”。
5. timing marks 是 scene-relative 还是 global-relative、如何叠加 scene start/lead、缺失/重叠/越界 marks 如何处理，尚未形成统一契约；这也依赖前述 timeline 产物修复。

### 3.12 P0-11：真实 Provider smoke 与故障恢复发布门禁

**判定：已落实（制度设计），执行状态未验证。**

- 每个发布候选必须通过不可跳过的真实 smoke（L748～750）。
- smoke 覆盖 Ollama、目标 edge-tts 音色、精确 FAL 队列/下载、ffmpeg/libass/CJK、faster-whisper 和 30～60 秒完整成片（L759～765）。
- intent/UUID/request_id/unknown-submit、已有 request_id 只查询、费用封顶与孤儿清理均已写入（L661～668）。
- 故障注入矩阵和验收不变量较完整（L764～767、L777）。

但当前仓库没有 CI、测试或结果，故只能确认“门禁已写入方案”，不能称“真实 smoke 已通过”。此外，P0-09 未冻结使 FAL smoke 暂无唯一目标；`unknown_submit` 未进入请求状态机；还需定义凭据注入、预算、报告留存、发布候选与 smoke 结果绑定、以及禁止手工 override 的机制。

## 4. P1-01～P1-12 吸收情况

| 原意见 | 复审判定 | 说明 |
|---|---|---|
| P1-01 ingest/复杂解析 | **部分合理吸收** | magic/MIME、稳定窗口、复制哈希、资源限额、逐页 OCR、多栏/DOCX 和测试矩阵已加入（L203～244）。仍缺 manifest Schema、内存上限、PDF 旋转文本、DOCX 页眉页脚；L242 要扫描/OCR 矩阵全绿，AC-01 L773 又写 OCR“另计”，验收边界不一致。 |
| P1-02 原文视觉优先 | **核心已合理吸收，元数据不足** | 六类 `visual_source` 和确定性叠字已加入（L321～334）。但 source page/block/bbox、crop 参数、版权/来源、最终资产 hash 没有完整契约；`extracted_ref` 仍只是字符串。 |
| P1-03 FAL 队列/缓存/成本 | **大部分合理吸收** | canonical JSON + SHA-256、全局缓存、seed/request_id、幂等/unknown-submit、成本上限已写（L362～367、L537、L663～688）。仍缺队列轮询 deadline、取消/下载校验流程，以及实际价格、许可证、MIME/尺寸/hash 等完整资产 metadata。 |
| P1-04 实测时长/混音 | **部分吸收，新增硬伤** | 实测时长、timeline、ducking、最终混音后响度和 timeline QC 已写（L353、L389、L396～397、L414）。但 C.3 sidechain 接反且 loudnorm 只一遍；实测超限后从 P5 反向触发 P3/P4/P5 重写的状态边未定义。 |
| P1-05 错误分类/重试 | **基本合理吸收** | Retry-After、jitter、attempt history、幂等、孤儿请求和预算已写（L640～668）。仍缺单调用/单场景墙钟 deadline；401/403 同时出现在 terminal 与 auth/quota 语义，需统一；429 限流与额度耗尽也应区分。 |
| P1-06 QC 硬失败/警告 | **部分吸收** | decode-to-null、规格、timeline、静音、blackdetect、字幕、事实和占位检查已加入（L410～421）。但缺可执行阈值矩阵；时长 ±5%、静音总比/最长段、黑帧、字幕覆盖、重复图等没有明确 fail/warn；最终 LUFS/true peak/削波未进入 P8；哪些状态允许发布不明确。 |
| P1-07 不可信输入/watch/子进程 | **大部分合理吸收** | 限额、稳定复制、magic/hash、shell=False、路径 containment 已写（L205～210、L402、L634～638）。仍缺 symlink/junction 解析后的 containment、内存限额、稳定窗口细则；L637 的“Job staging”与 L530 的成片 `staging/` 同名；外部 BGM/仓库字体又与“所有路径必须位于 Job 根”冲突。 |
| P1-08 可复现环境/依赖 | **部分吸收，当前仅为计划** | uv、PowerShell、python-dotenv、doctor、ffmpeg 功能探测和 fonts 目录已写（L448～486、L723～751）。但仓库没有 uv.lock/fonts/contracts/schemas/prompts/fixtures；Ollama 没有 model digest；FAL 未冻结；`winget install ffmpeg` 仍不复现 8.1.2 精确 build。 |
| P1-09 工期/关键路径 | **框架已吸收，数字仍矛盾** | 三档工期、M0～M7 和 0.5～2 人日工作包已加入（L698～721）。但 M0～M4 合计 13～20 人日，即约 2.6～4 周，不等于 PoC 的 2～3 周；MVP 定义为 M0～M7，而 M6/M7 已含安全/运维/smoke，发布级又把同一范围列为 MVP 之外，范围重复。 |
| P1-10 风险与触发器 | **风险类别已吸收，触发器未吸收** | 风险扩到 R20，类别明显完整（L790～811）。表格仍只有概率/严重度/笼统对策，缺责任人、检测信号、触发阈值、预防、恢复、残余风险；许可证仅覆盖运行时依赖，未完整覆盖输入图片、BGM、打包字体、音色/声音和生成内容权利。 |
| P1-11 磁盘/缓存/日志/保留 | **部分吸收** | 缓存校验、LRU/TTL、stats/gc/archive、磁盘 doctor 和保留原则已写（L537、L629、L682、L688、L742～743）。仍无具体配额、最大 workspace 数、TTL/失败产物保留时长、运行前磁盘阈值、日志字段/轮转/容量，以及“只留 final + 审计 manifest”模式。 |
| P1-12 自动分集 | **有意放弃且理由成立** | 已明确自动分集为二期（L53），一期超限拒绝或人工拆分（L62、L795）。这是原意见允许的合理范围收缩，P1-12 可关闭。 |

P1 总结：没有发现其他被明确“有意放弃”的 P1；P1-12 是唯一清楚、合理的延期。P1-02、P1-03、P1-05 的核心方案可视为合理吸收；其余多项仍是“方向吸收、验收或数据契约未闭合”，不应在 L5/L908 统称“全部 P1 已落实”。

## 5. P2 数量纠偏及逐项状态

原 `review_report.md` 实际有 **P2-01～P2-08，共 8 项**（原报告 L493、497、501、505、509、513、517、521），并非 2 项。v0.2 附录 D 的“11 P0 / 12 P1 / 2 P2”（L908）以及本次提交说明中的“2 项 P2”均与原报告不一致。看起来 v0.2 只把最前两项当成了计数，但实际上其余六项也被不同程度引用。

| 原 P2 | 复审判定 | 说明 |
|---|---|---|
| P2-01 Pydantic 单一来源 | **设计层部分吸收** | 单一来源、Draft 2020-12、CI 防漂移已写（L175、L452、L539～587、L762）；但没有模型/生成文件，且 P4 示例不满足 Schema。 |
| P2-02 append-only events | **已合理吸收** | events.jsonl 明确用于审计/重建，未替代 state、锁和原子提交（L105、L181、L519、L602～606）。需补尾部损坏恢复细节，但原建议方向已落实。 |
| P2-03 跨场景视觉一致性 | **部分吸收** | 固定 model/seed、style bible、色板后处理已写（L333）；仍缺参考图/可控编辑端点和可执行一致性指标。 |
| P2-04 中文字幕/无障碍 | **大部分吸收** | CJK 字体、行长、两行、最小时长、断句、混排、发音词典、安全区已写（L381）；平台 UI 避让和字体许可证/缺字降级仍缺。 |
| P2-05 冷热缓存/分位数 SLO | **部分吸收且口径错误** | AC-07 有冷/热、短文/5000 字、横/竖与 P50/P95（L779），但没有任何目标值；又把云排队时间排除“端到端”，与原意见“默认路径依赖云队列时不得排除”相反。应同时报告组件耗时和包含排队的真正 end-to-end。 |
| P2-06 不引入重编排平台 | **已合理吸收** | asyncio 的适用边界及以后触发 Prefect/Temporal 的条件写得合理（L451）。 |
| P2-07 统一编号/术语/示例 | **大部分吸收，但仍有新不一致** | Scene/Cue、缓存命名和阶段编号已改善；P2 总数、示例时长、Schema/实例、timeline 归属等又产生新不一致。 |
| P2-08 文档自包含定位 | **有意降级定位，但当前表述仍不成立** | L7/L913 改为“文档 + 仓库内契约/Prompt/样例”，这是合理的定位调整；然而这些仓库资产目前不存在，B.3 仍写“全文略、版本化于仓库”（L868），所以当前仓库仍不能构成所称开发依据。 |

## 6. v0.2 新增或遗留的硬伤、矛盾与编号问题

### 6.1 阻断级问题

| # | 问题 | 证据 | 后果 | 必须修改 |
|---|---|---|---|---|
| H-01 | 唯一 Schema 与唯一示例不兼容 | P4 示例 L306～318；required L567～579 | 文档按自身契约即失败；无法做契约测试 | 让示例通过生成 Schema；提交全部模型/Schema/fixtures/CI |
| H-02 | FAL “已冻结”与“待冻结”并存 | L13/L63/L359 对比 L448/L712 | Provider 客户端、缓存键、成本和 smoke 无唯一目标 | M0 Spike 产出唯一 endpoint/config/Schema/hash/价格/核验日期后再关闭 P0-09 |
| H-03 | BGM sidechain 图接反 | L890～893；FFmpeg main/sidechain 定义 | 不 duck BGM，反而压缩并叠加旁白 | 修正 filter graph，并用合成音频 fixture 检测旁白/BGM增益 |
| H-04 | 两遍 loudnorm 与附录一遍冲突 | L397 对比 L892 | 无法复现响度目标；正文与实现模板不一致 | 给出两遍测量/应用流程或明确采用的等价算法和测量记录 |
| H-05 | 状态/请求状态机不闭合 | L595～600 对比 L632/L665 | warnings 无法向下游推进；unknown-submit 无法持久化/恢复；失败后重试无合法边 | 分离 stage state 与 request state，补完整转移、不变量、退出码和人工批准流程 |
| H-06 | resume 没有全量重验算法 | L600～607 | succeeded 文件被删/截断/替换仍可能被跳过，原 P0 风险仍在 | 明确 P0 起逐阶段实际 hash/Schema/语义/媒体复核与最早 dirty 级联 |
| H-07 | render_timeline 的生产顺序循环 | P6 先于 P7（L145～170），timeline 隐含在 P7 render manifest（L529），P6 又要求共用（L389） | P6 无法引用尚未存在的“单一事实源”，字幕/画面可各算各的 | 在 P5 后提交独立、不可变、带 Schema/hash 的 timeline，再让 P6/P7/P8 引用 |
| H-08 | 隐私默认与授权规则不闭合 | A6 L64、默认 approved_cloud L675、只管“降级”L681 | 敏感文档可能在任何分类/授权前把旁白与 prompt 发往默认云 Provider | 默认 offline 或先分类/授权；默认主调用和降级调用使用同一授权门禁 |
| H-09 | P0 manifest 无法记录“实际外发” | P0 manifest L210、不可变原则 L176、实际清单 L678 | 若回写则破坏不可变；若不回写则审计承诺无法实现 | 增加后置 `egress_manifest`/事件契约，不回写 P0 manifest |
| H-10 | 事实引用只证明 ID 存在，不证明主张受支持 | L294、L589、L776 | 可能带着任意有效 block ID 编造句子仍通过 | 引入 fact/claim ID、规范化值/单位/极性和 claim→fact→source 验证 |

### 6.2 重要一致性问题

1. **TTS marks 契约自相矛盾：** L350/FR-05 要所有 Provider 在 P5 输出 marks，L378 又承认 Provider 可能无原生 marks并在 P6 对齐。应区分 `native_marks?` 与 P6 的必填 `aligned_marks/cues`。
2. **OpenAI TTS 能力误标：** L347 的“原生 speech marks ✅”没有当前官方公开契约依据；应改成“待 M0 contract smoke / 默认走 aligner”。
3. **QC 仍不可执行：** L410～421 没有为时长、静音、黑帧、字幕覆盖、重复图、LUFS/true peak/削波给完整阈值和 pass/warn/fail/needs_review → publish 矩阵。
4. **P7 成片被移动后的产物一致性：** P7 提交 staging 成片与 render manifest，P8 用 `os.replace` 移到 final 后，P7 原路径可能消失。resume 复核会把 P7 判 dirty。应让 P8 拥有 release manifest，或采用不会破坏 P7 已提交产物的发布协议。
5. **P9 与 QC 不可变性冲突：** P9 在 P8 之后，L431 又说 P9 失败“记录到质检报告”。`qc_report.json` 是 P8 已提交的不可变产物，P9 不能回写；应写独立 `draft_export_report.json` 或把 P9 前置且明确非门禁。
6. **真实端到端 SLO 定义错误：** L779 排除云排队却仍称端到端；并且只说“达标”而无目标数值，AC-07 不能判定。
7. **工期算术/范围矛盾：** M0～M4 是 13～20 人日（2.6～4 周），不等于 PoC 2～3 周；M0～M7 已含 M6 安全运维和 M7 smoke，发布级又将这些列为 MVP 之外。
8. **OCR 验收矛盾：** L242 要扫描/混合 OCR 测试矩阵必须全绿，AC-01 L773 又把 OCR 样例“另计”；应明确 PoC/MVP/发布级分别包含什么。
9. **路径 containment 与外部资产冲突：** L402 要路径留在 Job 根，但 CLI 接受任意 BGM 路径（L621），字体位于仓库 `fonts/` 而非 Job 根（L514/L899）。应在 P0/P5 把外部 BGM/字体快照进 Job，或定义只读可信根集合。
10. **`staging` 名称复用：** watch L637 的“复制到 Job staging”与 L530 的“未过门禁成片 staging”不是同一生命周期。入口临时区应改为 `ingress_tmp/` 或直接使用 P0 的 input 临时文件。
11. **附录示例不满足自身讲稿规则：** P3 要每场景 60～180 字（L292、L843），附录 P3 两个 narration（L823～826）均明显不足 60 字；若为节选，应明确字段内容也被截断、不可作为可校验 fixture。
12. **37 秒并非 1～10 分钟目标区间：** A4 明确目标 1～10 分钟（L62），L834 却称约 37 秒“目标区间内”。可以允许低于非硬下限，但不能称其位于区间内。
13. **Schema 约束与正文范围不一致：** 正文 60～180 字、约 13～40 秒，Schema 却允许 narration 20～500 字、duration 5～60 秒（L571～572）；planned scenes 最大 20（L269），Schema 最大 60（L564）。如为不同层级上限，需明确说明。
14. **风险扩到 R20 但未完成原要求：** 缺 owner、trigger、signal、recovery、residual risk；R17 只覆盖依赖许可证，未覆盖 BGM、打包字体、提取图片、生成图/音色权利。
15. **编号/计数错误：** 原报告有 8 项 P2，v0.2 L908 写 2 项。L5/L908 的“全部 P0/P1 已修订”也与本复审证据不符。
16. **“仓库内已版本化”表述失实：** L7、L507、L509～514、L751、L868、L913 均依赖尚不存在的 contracts/schemas/prompts/config/fonts/fixtures/uv.lock；当前应使用将来时，或先提交后再声称。

## 7. 剩余修改清单

### 7.1 再次复审前必须完成（P0）

1. **提交并验证全部契约。** 提交十类核心 Pydantic 模型、生成的 Draft 2020-12 Schema、artifact/request/egress/release manifest Schema、示例实例、跨文件语义验证器和 CI 漂移检查；所有文内/仓库样例必须真实通过。
2. **冻结唯一 FAL 契约。** M0 Spike 选定一个默认 endpoint/model_id 与调用通道，保存 Schema/hash、默认值/限制、价格、留存控制、核验日期和 contract smoke；删除“已冻结/待冻结”并存。
3. **修复并真实验证 FFmpeg 链。** 修正 sidechain 输入、两遍 loudnorm、fade 公式、level/timebase/stereo；用短 fixture 实际跑横屏、竖屏、含空格/Unicode 路径、字幕字体、BGM ducking，并由 ffprobe、decode-to-null、响度测量验证。
4. **冻结 timeline 与发布产物归属。** 在 P5 后提交独立 timeline；P6/P7/P8 只引用该版本。定义 staging → final 不破坏 P7 已提交产物的 release manifest/原子协议。
5. **补完 stage/request 状态机与 resume。** 纳入 warnings/needs_review/failed/cancelled/unknown-submit、人工批准、重试、失效和退出码；逐阶段实际重验全部 succeeded 产物，按最早 dirty 节点级联。
6. **完善事实追溯契约。** 定义稳定 block_id、fact_id、规范化数字/日期/单位/极性、claim→fact/source 关系、coverage 计算和高风险人工审批；把日期、单位、否定关系写进 AC/QC。
7. **把 privacy_mode 变成可执行门禁。** 提交 Provider 信任矩阵、数据分类、授权 receipt、默认策略、FAL/其他 Provider 留存参数、egress manifest、工作区/缓存保留与删除规则；默认主路径和 fallback 同等受控。
8. **冻结 LLM/tokenizer/资源配置。** 指定 tokenizer 与 revision、num_ctx、输出预算、安全余量、model digest/量化/temperature/seed/structured mode；doctor/preflight 必须能证明实际 NUM_PARALLEL 与 GPU 路径满足配置。
9. **修正 TTS/字幕 Provider 契约。** 将原生 marks 设为 capability/可空字段；冻结 forced aligner；更正 OpenAI TTS 能力；定义 marks 规范化、scene/global 时间转换和低置信 fallback 的 `needs_review/preview-only` 状态。
10. **把 release smoke 绑定到发布候选。** 定义不可 override 的执行位置、凭据、费用预算、结果/日志留存、候选 artifact digest 与 smoke report 绑定；提交第一次真实结果后才能称“已验证”。

### 7.2 应在 v0.3 同步完成（P1）

1. 给 QC 建完整数值门禁矩阵，加入最终 LUFS、true peak、削波、字幕覆盖、静音/黑帧、占位/重复图和发布状态。
2. 补输入资源限额的实际默认值、OCR 验收边界、symlink/junction 与路径根策略、入口临时目录。
3. 完成视觉资产 provenance/crop/license/hash metadata、FAL 队列 deadline/取消/下载校验/实际成本记录。
4. 重画 PoC/MVP/发布级与 M0～M7 的映射，使人日合计、并行假设、缓冲和范围不重复。
5. 风险表加入 owner、signal、trigger、prevention、recovery、residual risk，并补内容/BGM/字体/声音/生成素材权利。
6. 给磁盘、workspace、cache、日志、失败产物设置可测试的默认配额、TTL、轮转、归档和最小保留模式。
7. 修正 P2 总数、37 秒示例、讲稿长度、Schema 上限、OCR、`staging`、P9 报告等全部文本不一致。
8. 在资产尚未提交前，把“已生成/已入库/共同构成开发依据”改为未来时；提交后再恢复完成时表述。

## 8. 复审关闭条件

下一版要达到“有条件通过”，至少应同时满足：

- 上述 7.1 的十项全部在文档和仓库产物上闭合；
- 所有 JSON 示例实际通过生成 Schema，所有跨文件引用集合通过语义验证；
- FFmpeg 横/竖屏、ducking、两遍响度、字幕和最终规格 smoke 真实通过；
- FAL 唯一 endpoint 及 TTS/aligner 能力完成低成本真实 contract smoke；
- 故障注入证明截断/丢失/替换的 succeeded 产物会被发现，既有 request_id 不重提，unknown-submit 有可审计处置；
- privacy/授权/egress/保留链可审计，敏感文档不会在授权前走默认云路径；
- QC 阈值和发布状态矩阵可机器判定，`needs_review/failed/cancelled/preview` 不可能晋升 final；
- 附录 D 正确记录原报告为 11 P0 / 12 P1 / 8 P2，并如实记录本次结论。

在此之前，允许开展 M0 Spike 和受控 PoC 风险验证，但不应把 v0.2 标记为“复审通过”或“全部 P0/P1 已关闭”。

---

最终结论再次确认：**不通过。** v0.2 的主架构已显著改善，但仍存在可复现的媒体链硬错、未提交且自相矛盾的契约、未冻结的 Provider，以及状态/隐私/追溯闭环缺口；完成剩余 P0 后再提交 v0.3 复审。
