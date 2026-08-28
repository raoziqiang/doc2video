# doc2video 代码审查结果报告 (v03)

- 审查日期：2026-08-28
- 审查范围：HEAD 工作树全量源码，重点 `src/doc2video/`、`tests/`，其次 `scripts/`
- 基线状态：156 个测试全部通过；审查发现问题 10 项（1 严重 / 6 中等 / 3 轻微），其中 2 项已实际复现崩溃
- 修复状态：**10 项全部修复**，修复后 164 个测试通过（新增 8 个回归测试），`ruff check` 全绿

---

## 一、总体评价

代码整体质量较高：状态机 + 原子提交 + 产物哈希复核 + fail-closed QC 硬门禁的架构执行一致；
路径穿越防护、ZIP bomb 限额、ASS/concat 转义、无 `shell=True` 的 subprocess 用法等安全细节到位。

主要短板集中在两类（恰好是测试覆盖盲区），本次已全部闭环：

1. **异常/失败路径的续跑闭环**（问题 1、2、6）
2. **声明了但未实现的选项与审计机制**（问题 3、4、5）

---

## 二、问题清单与修复详情

### 🔴 严重（1 项）

#### 1. `failed` 阶段无法续跑：`resume` 抛出未捕获的 `StateError` 崩溃

- **位置**：`src/doc2video/pipeline/runner.py` `run_stages`
- **问题**：只把 `invalidated | needs_review` 重置回 `pending`，而转移表规定 `failed → running` 非法（须先 `failed → pending`）。任何曾失败的阶段在 `resume` 时直接崩溃。
- **复现**：P0/P1 succeeded、P2 failed 时调用 `run_stages` → `StateError: 非法状态转移: failed -> running`
- **影响**：与"崩溃安全、断点续跑"的核心设计直接矛盾，是续跑主路径上的硬缺陷。
- **修复**：重置集合扩展为 `invalidated | needs_review | failed | cancelled`，先合法转移到 `pending` 再进入 `running`。
- **验证**：新增 `test_resume_after_failed_stage_reruns`（模拟 P2 失败后 resume 重跑成功）。

### 🟡 中等（6 项）

#### 2. `resume` 在 P0 未完成时无条件读取 `manifest.json` → 裸 `FileNotFoundError`

- **位置**：`src/doc2video/cli.py` `cmd_resume`
- **修复**：读取前检查存在性，缺失时输出明确错误并返回退出码 2（提示重新运行 `run`）。
- **验证**：新增 `test_resume_missing_manifest_fails_cleanly`。

#### 3. 多个 CLI 选项被静默忽略：`--no-burn-subs`、`--bgm`、`--max-duration`、`--llm`、`--jobs`

- **位置**：`cli.py` `RunOptions` 收集但全仓库无消费方
- **修复**（逐项落实，均不静默）：
  - `--max-duration`：P5 计算时间轴后强制总时长上限（缺省取 `video.max_duration_s`），超限 `AssetError` 硬失败；
  - `--no-burn-subs`：P7 跳过 ASS 烧录，成片直接取混音/归一后画面，ASS 保留供软字幕使用；
  - `--bgm`：P7 新增 `_mix_bgm`，接入原死代码 `build_audio_mix_filter`（旁白为 sidechain 压抵 BGM，BGM 循环至全片长度），BGM 文件不存在时报错；
  - `--llm cloud`：未实现 → `cmd_run` 显式拒绝（退出码 2，fail closed）；
  - `--jobs > 1`：批量并行未实现 → 打印降级警告后串行执行。
- **验证**：新增 `test_run_max_duration_enforced`、`test_run_llm_cloud_fails_closed`。

#### 4. 外发审计形同虚设：`egress_manifest.json` 从未写入，云调用配额从未强制

- **位置**：`p5_assets.py`、`p8_qc.py`、`config/default.yaml`
- **修复**：
  - P5 新增 `_load_egress / _assert_egress_quota / _record_egress`：FAL 图像生成与 edge-tts 调用**前**校验 `max_cloud_calls_per_job` 配额（超限 `AssetError` 拒绝），**成功后**追加原子写 `egress_manifest.json`（含 `client_request_uuid`；FAL 请求同时携带 `x-fal-client-request-uuid` 幂等头）；
  - P8 新增"外发审计"QC 检查：汇总进 `egress_report.json`，且 **offline 作业出现任何云调用即硬失败**（隐私违规门禁）。
- **遗留**：`max_cost_per_job / max_cost_per_day` 成本配额未强制——FAL/edge-tts 响应不含费用数据，无可靠计费来源；待接入计费接口后补齐。
- **验证**：新增 `test_egress_quota_fail_closed_and_audited`。

#### 5. offline 隐私模式仍会发生未审计的外部网络下载

- **位置**：`providers/token_counter.py`（经 `llm_ollama.count_tokens` 被 P2 调用）
- **问题**：本地无缓存时从 `huggingface.co / hf-mirror.com` 下载 `tokenizer.json`，不感知 `privacy_mode`，违反 offline 无网络承诺。
- **修复**：`count_tokens(texts, allow_network=)` 全链路贯通（`base → OllamaLLM → TokenCounter`）；P2 按 `privacy_mode != "offline"` 传参；无缓存且禁网时直接走保守估算兜底（1 字符 ≈ 1 token，安全侧）。已有本地缓存时照常使用，不发网络请求。
- **验证**：新增 `test_token_counter_offline_forbids_download`（下载函数置为炸弹，确认绝不触发）。

#### 6. `resume` 丢失原始运行参数，云模式作业续跑静默降级为 offline

- **位置**：`cli.py` `cmd_resume`
- **修复**：
  - `cmd_run` 在建目录后原子写 `run_options.json`（全部生效参数）；
  - `cmd_resume` 经新增 `_load_run_options` 恢复；`privacy_mode` 以不可变 manifest 为准，与 `run_options` 不一致时打印警告；文件缺失/损坏（旧作业）时警告并使用默认参数；`llm=cloud` 续跑显式降级为 local 并提示。
- **验证**：新增 `test_run_persists_run_options_for_resume`。

#### 7. `rendered_table` 寻址错误：可能把段落块渲染成"表格图"

- **位置**：`p4_scene_plan.py` `choose_visual_source`、`p5_assets.py`
- **问题**：两处都无条件取 `source_block_ids[0]`；引用顺序为"段落 + 表格"时，画面会把段落文字渲染成表格图，与旁白事实不符，且 P8 门禁无法发现画面级错误。
- **修复**：P4 用 `next(bid for bid ... if block_types[bid] == "table")` 冻结真正的表格块寻址名；P5 同步按块类型（从 `parsed.json` 构建 `block_types`）选取，无表格块时回退首个引用块并追加 warning。
- **验证**：新增 `test_visual_source_rendered_table_picks_real_table_block`。

### 🟢 轻微（3 项）

#### 8. `cmd_doctor` 执行了两遍全部检查

- **修复**：检查结果只跑一遍并复用，`X 项检查，Y 项失败` 不再自相矛盾，探测开销减半。

#### 9. P3 对 `set` 迭代导致 Prompt 顺序跨进程不确定

- **修复**：`list(dict.fromkeys(plan.section_ids))` 保序去重，恢复"同版本同输入可复现"与缓存价值。

#### 10. 跨作业共享缓存的写入非原子

- **修复**：`CacheStore.put` 改为先写临时文件再 `os.replace`（并发读者永远看不到半成品）；已有合法条目（`get` 校验通过）不重写。

---

## 三、修改文件清单

| 文件 | 变更摘要 |
|---|---|
| `src/doc2video/pipeline/runner.py` | failed/cancelled 纳入续跑重置集合（问题 1） |
| `src/doc2video/cli.py` | resume 健壮性、`run_options.json` 持久化与恢复、doctor 单次、cloud 拒绝、jobs 警告（问题 2/3/6/8） |
| `src/doc2video/pipeline/p5_assets.py` | egress 审计与配额、表格块选取、时长上限（问题 3/4/7） |
| `src/doc2video/pipeline/p7_render.py` | `--no-burn-subs`、`--bgm` 消费（问题 3） |
| `src/doc2video/pipeline/p8_qc.py` | 外发审计 QC 门禁（offline 零云调用）（问题 4） |
| `src/doc2video/providers/token_counter.py` `llm_ollama.py` `base.py` | `allow_network` 全链路（问题 5） |
| `src/doc2video/pipeline/p2_understand.py` | offline 禁网传参（问题 5） |
| `src/doc2video/pipeline/p4_scene_plan.py` | 表格块寻址（问题 7） |
| `src/doc2video/pipeline/p3_script.py` | 保序去重（问题 9） |
| `src/doc2video/cache.py` | 原子写（问题 10） |
| `tests/test_cli.py` | +5 回归测试；补 `StageStatus` 导入 |
| `tests/test_m4_p5.py` | +1 回归测试；fake 适配器签名同步 |
| `tests/test_m3_p4.py` | +1 回归测试 |
| `tests/test_providers.py` | +1 回归测试；stub 签名同步 |
| `tests/fake_llm.py` | `count_tokens` 签名同步 |

## 四、测试与验证结果

- **修复前**：156 passed
- **修复后**：**164 passed**（+8 个回归测试，覆盖问题 1/2/3/4/5/6/7 的关键路径），0 failed
- **静态检查**：`ruff check src tests scripts` → All checks passed

| 新增测试 | 覆盖问题 |
|---|---|
| `test_resume_after_failed_stage_reruns` | 问题 1 |
| `test_resume_missing_manifest_fails_cleanly` | 问题 2 |
| `test_run_max_duration_enforced` | 问题 3（时长） |
| `test_run_llm_cloud_fails_closed` | 问题 3（cloud） |
| `test_run_persists_run_options_for_resume` | 问题 6 |
| `test_egress_quota_fail_closed_and_audited` | 问题 4 |
| `test_visual_source_rendered_table_picks_real_table_block` | 问题 7 |
| `test_token_counter_offline_forbids_download` | 问题 5 |

## 五、遗留事项（建议后续跟进）

1. **成本配额**：`max_cost_per_job / max_cost_per_day` 需要接入云服务商计费数据后才能强制，目前仅 `max_cloud_calls_per_job` 生效。
2. **`--jobs>1` 批量并行**：当前仅警告降级，待 batch 功能落地。
3. **cloud LLM provider**：`--llm cloud` 已改为显式拒绝，待真正实现云 provider 后放开。
4. **缓存并发竞态窗口**：`put` 原子化后仍存在极小的"内容已替换、marker 未写"窗口（并发读会视为损坏并清除后 miss），单作业串行下不触发；`--jobs>1` 落地前建议再加固。
