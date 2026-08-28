# doc2video 代码审查报告（v0.1）

- **审查对象**：`src/doc2video` 实际实现代码（38 个 Python 文件），对照《技术方案.md》v0.2.1
- **审查日期**：2026-08-27
- **审查方式**：逐文件人工阅读 + 全仓 grep 交叉验证 + 运行测试基线
- **基线验证**：`uv run pytest -q` → **156 passed**；`uv run ruff check src tests scripts` → **All checks passed**

---

## 0. 总体结论

**结论：不通过（NO-GO）。**

工程质量与测试纪律明显优于典型项目（原子提交、fail-closed、契约 extra=forbid、事件审计日志全部真实落地，156 个测试全绿），但存在**多个 CLI 承诺的功能在代码中完全没有实现**的问题——`--bgm`、`--no-burn-subs`、`--max-duration`、成本上限、egress 审计、native marks 主路径均属于"接口存在、实现缺失"或"实现存在、接线缺失"。这类问题对用户是直接的功能欺骗：命令行接受参数、不报错、静默忽略。此外 resume 语义存在两处实质缺陷（丢失原始运行选项、manifest 缺失时崩溃）。

与历史两轮文档评审（review_report.md / review_report_v02.md）不同，本轮确认了 v0.2 中多项 H 级阻断项**已经在代码中真实修复**（详见第 2 节），但新的缺口集中在"最后一公里接线"层面。

---

## 1. 优点（设计符合度确认）

以下设计承诺经代码验证**已真实落实**：

| 设计承诺 | 实现位置 | 验证结论 |
|---|---|---|
| 原子提交协议（tmp → flush → fsync → os.replace） | state.py | ✅ 完整实现 |
| Job 跨进程排他锁（msvcrt/fcntl 双平台） | state.py | ✅ 实现正确 |
| events.jsonl 审计日志 + 校验和 | state.py | ✅ 实现，但 append 性能见 L-02 |
| 状态转移表 TRANSITIONS + require_transition | state.py | ✅ 实现 |
| render_timeline 由 P5 提交（H-07 修复） | p5_assets.py | ✅ 落实 |
| 两遍 loudnorm（H-04 修复） | p7_render.py | ✅ 落实，实测结果入 render_manifest |
| sidechain ducking 方向正确（H-03 修复） | p7_render.py `build_audio_mix_filter` | ✅ BGM 为 main、旁白为 sidechain，滤镜本身正确 |
| 隐私默认 offline + 占位素材 → needs_review（退出码 3） | p5/p8/runner | ✅ fail-closed 落实 |
| P8 硬门禁不过不晋升 final（硬链接晋升） | p8_qc.py | ✅ 落实 |
| P9 不回写 P8 产物 | p9_jianying.py | ✅ 落实 |
| 契约 extra=forbid + Draft 2020-12 Schema 生成 | contracts/ | ✅ 落实 |
| 内容寻址缓存 + .sha256 marker 校验 | cache.py | ✅ 落实 |
| P0 DOCX ZIP bomb 防护、magic 嗅探 | p0_ingest.py | ✅ 落实 |
| P3 check_claims 确定性一致性检查（quote 逐字 + 关键 token） | p3_script.py | ✅ 落实 |
| P9 路径 containment（拒绝穿越） | p9_jianying.py | ✅ 落实 |

测试纪律值得肯定：测试覆盖了 resume 幂等、脏产物检测、契约校验、滤镜字符串等关键逻辑。问题不在"表面质量"，而在**功能完整性与宣称一致性**。

---

## 2. 问题清单 — P0 阻断（必须修复）

### B-01 `--bgm` 功能整体未实现（死代码）
- **证据**：`build_audio_mix_filter`（p7_render.py）从未被 stage_p7 调用。CLI 接受 `--bgm` 并写入 opts，但渲染管线完全不读取。grep 全仓：唯一调用方是 tests/test_m6_p7.py 的单元测试（仅测字符串格式）。
- **影响**：用户传 `--bgm` → 静默产出无声视频。功能欺骗。
- **修复**：stage_p7 中 probe BGM 时长 → 构造 filter → 混入 filter_complex，与两遍 loudnorm 衔接。

### B-02 `--no-burn-subs` 未实现
- **证据**：p7_render.py 中 grep `no_burn_subs` / `burn` 零命中（滤镜构造处）。P7 无条件构造 ASS 烧录命令。
- **影响**：CLI 选项静默忽略，永远烧录字幕。

### B-03 `--max-duration` 无任何执行点
- **证据**：`max_duration` 仅存在于 cli.py 默认值与 config/default.yaml（`max_duration_s: 600`），src 中无比较逻辑。
- **影响**：超长视频无法拦截；P8 的"时长±5%"检查只验讲稿时长一致性，不验总时长上限。

### B-04 egress_manifest.json 无写入方（隐私审计落空）
- **证据**：grep 全仓，唯一读取方 p8_qc._write_egress_report，唯一"写入方"是测试文件。真实运行中 egress 报告恒为空。
- **影响**：直接违反方案 7.4 隐私三档与 egress 审计闭环（对应历史 H-09）。offline 模式的承诺"可审计"不成立。
- **修复**：FAL/edge-tts/whisper 每次外部调用处（p5_assets.py、p6_subtitles.py）追加 egress 记录。

### B-05 native_marks 无生产方 → P6 主路径不可达
- **证据**：edge-tts 调用（p5_assets.py）未收集 WordBoundary 事件；`native_marks` 唯一生产方是 tests/test_m5_p6.py。
- **影响**：P6 三级策略（方案 4.7）的"主路径 native marks → whisper 兜底"实际退化为**CPU whisper 唯一路径**——14 分钟视频可能多耗 5-15 分钟 CPU 时间且分句质量下降。
- **修复**：edge-tts 请求 SubMaker/WordBoundary 流，落盘 audio/marks.json，P5 写入 narration 的 native_marks 字段。

### B-06 cmd_resume 丢失原始运行选项
- **证据**：cli.py `cmd_resume` 中 `opts = RunOptions(privacy_mode=cfg["privacy"]["default_mode"])`——style/aspect/voice/bgm/preview 全部重置为默认。
- **影响**：resume 出来的视频与首跑规格不一致（竖屏变横屏、换音色）。违反 resume 幂等语义。
- **修复**：P0 阶段把 RunOptions 序列化进 job 目录（如 run_options.json），resume 时读取。

### B-07 FAL request_id 不预持久化（崩溃后重复扣费）
- **证据**：p5_assets.py FAL 提交后才有 request_id，提交前无落盘。崩溃发生在 submit → 响应返回之间时，resume 无法得知已提交的 request_id，会重新提交（重新计费）。
- **影响**：违反方案 7.3 错误处理（request_id 持久化/不重提）与历史 P0-11。
- **修复**：提交前先写 pending 状态（含 prompt hash），提交后立刻补记 request_id。

### B-08 成本/调用上限配置存在但零执行
- **证据**：config/default.yaml 中 `max_cloud_calls_per_job: 60`、`max_cost_per_job: 5.0`、`max_cost_per_day: 20.0`，grep src 零命中。
- **影响**：成本护栏完全失守——一个失控任务可以无限调用 FAL。

---

## 3. 问题清单 — P1 高危

### H-01 stage_fingerprint 为桩级，resume 全量重验不比对指纹
- **证据**：runner.py `_stage_fingerprint` 仅含 stage+config+version，注释自认"M1 起补充输入哈希/Prompt"；verify_and_invalidate 只验产物哈希，不验指纹。
- **影响**：改配置后 resume 不失效旧产物——与新配置语义不一致的产物直接晋升。

### H-02 cmd_resume 在 manifest.json 缺失时崩溃
- **证据**：cli.py：P0 非终态且 manifest.json 不存在时 `Manifest.model_validate_json(...read_text())` 抛 FileNotFoundError，无友好提示。
- **影响**：resume 一个刚创建即崩溃的 job → traceback 而非引导重新 ingest。

### H-03 Ollama think 参数位置错误
- **证据**：llm_ollama.py `"options": {"num_ctx":..., "temperature":..., "think": False}`。Ollama /api/chat（v0.9+）要求 `think` 为**顶层**参数，放在 options 内会被忽略。
- **影响**：依赖空 content 检测兜底；cfg 中 `llm.think` 与 `max_output_tokens`（num_predict）读了配置却不发送。

### H-04 P2 的 NEEDS_REVIEW 信号被吞
- **证据**：p2_understand.py `raise ValueError("NEEDS_REVIEW: ...")`，runner 的 except 分支统一按 failed 处理（退出码 2），而非 needs_review（退出码 3）。
- **影响**：应人工复核的场景被标记为硬失败，自动化系统会误判为可重试/程序缺陷。

### H-05 P5/P4 的 `source_block_ids[0]` 假设
- **证据**：p4_scene_plan.py rendered_table、p5_assets.py 表格渲染均取 `source_block_ids[0]`，不校验该块是否真是 table 类型。
- **影响**：LLM 给出的第一个来源块若非表格 → 渲染非表格内容或 KeyError。

### H-06 P8 事实检查不验数字一致性
- **证据**：p8_qc.py _check_facts 只验 fact ID 是否存在于讲稿，不验数字/单位是否仍与源文档一致（P3 生成时验过，但 QC 复验缺失——若 P5 修复流程改写了讲稿则脱防）。
- 另外 P2 的 coverage.uncovered 不进入 P8 门禁。

### H-07 token_counter 运行时下载 tokenizer（offline 模式违反）
- **证据**：token_counter.py 从 HF 下载 tokenizer.json，无本地缓存检查；offline 隐私模式下 P2 仍会触发网络调用。
- **修复**：打包 tokenizer 或首跑缓存 + offline 时用字符估算兜底。

### H-08 doctor 双重执行 + 条件笔误
- **证据**：cli.py cmd_doctor 调用 `_doctor_checks(cfg)` 两次（全部网络探测跑两遍）；doctor 中 `("14b" in m or "14b" in m)` 重复条件笔误（疑似本意 `qwen3` 或第二个模型名）。
- 另：OLLAMA_NUM_PARALLEL 不足仅 warn 不 fail（违反方案 5.3 fail-closed）。

---

## 4. 问题清单 — P2 中低危

| ID | 位置 | 问题 |
|---|---|---|
| M-01 | p1_parser.py | RapidOCR 每个 OCR 页重新实例化（模型逐页重载，N 页 N 次加载） |
| M-02 | p1_parser.py | 加粗 span 误判为标题（中文文档内联加粗常见） |
| M-03 | p1_parser.py | PDF 内嵌图片不提取（仅 DOCX 提取）；MD 代码块/引用块静默丢弃 |
| M-04 | p2_understand.py | 分块 id `b{n}#{i}` 与归并时 `lstrip("b")` 脆弱解析；分块串行处理 |
| M-05 | p3_script.py | chapter_pages 死代码；无总时长约束（并入 B-03） |
| M-06 | p9_jianying.py | _rewrite_native_paths 只重写 draft_content.json，draft_meta_info.json 路径未重写（重命名后残留旧临时路径） |
| M-07 | p6_subtitles.py | _native_cues 循环内重复计算 split_caption（O(n²)） |
| L-01 | state.py | EventLog append 时 `seq = len(read()) + 1` 每次全量重读（O(n²)） |
| L-02 | runner.py | _commit_artifacts 中 revision 恒为 1（修订追踪名存实亡） |
| L-03 | p5_assets.py | `__import__("doc2video.contracts", ...)` 反模式 |
| L-04 | p0_ingest.py | 无 BGM/字体快照（--bgm 路径 containment 落空）；无稳定窗口等待 |

---

## 5. 与《技术方案.md》v0.2.1 对照差距表

| 方案条目 | 状态 |
|---|---|
| 4.7 字幕三级策略（native marks 主路径） | ❌ 主路径不可达（B-05） |
| 4.8 时间轴公式与两遍 loudnorm | ✅ 落实 |
| 6.3 状态机 + resume 全量重验 | ⚠️ 部分落实：产物重验有，指纹比对无（H-01） |
| 7.3 错误处理（request_id 持久化/不重提） | ❌ 未落实（B-07） |
| 7.4 隐私三档与 egress 审计闭环 | ⚠️ offline 默认落实，egress 审计空转（B-04） |
| 9.2 验收标准（QC 矩阵） | ⚠️ 矩阵完整，事实/coverage 门禁弱（H-06） |
| 5.3 Ollama 环境检查 fail-closed | ⚠️ 仅 warn（H-08） |
| 成本护栏（5.x） | ❌ 零执行（B-08） |

---

## 6. 修复优先级建议

**第一批（合并前必须，B-01 ~ B-08）**：
1. B-04 egress 审计写入（隐私承诺，法律层面）
2. B-07 request_id 预持久化（真金白银重复扣费）
3. B-08 成本上限执行（同上）
4. B-06/B-02 resume 丢 opts + manifest 崩溃（稳定性）
5. B-01/B-02/B-03 三个 CLI 静默忽略项（要么实现要么移除选项并改 README）
6. B-05 native_marks 生产（性能与质量）

**第二批（下个迭代，H-01 ~ H-08）**：指纹增强、think 顶层参数、NEEDS_REVIEW 通道、source_block_ids 类型校验。

**第三批（择机，M/L 项）**：OCR 实例复用、EventLog 增量 seq、死代码清理。

**测试建议**：当前测试大量使用"手工构造产物"验证管线（如 test_m5_p6 手工构造 native_marks），掩盖了"生产方缺失"问题。建议增加端到端冒烟测试（小输入 → 全管线 → 断言最终 mp4 含音轨/字幕轨），并对每个 CLI flag 增加"接线测试"（传参后断言下游行为改变）。

---

## 7. 审查方法说明

- 逐文件阅读 src/doc2video 全部 38 个 Python 文件（pipeline 11、providers 4、contracts 16、核心 7）
- 关键结论均经 grep 全仓交叉验证（如 egress_manifest 唯一写入方在 tests、build_audio_mix_filter 唯一调用方在 tests）
- 基线：`uv run pytest -q` 156 passed；`uv run ruff check src tests scripts` 全绿
- 对照基准：技术方案.md v0.2.1、README.md 宣称能力、历史评审 H-01~H-10 修复情况
