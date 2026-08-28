# 性能基准与 SLO（S3.4）

## 1. 测量矩阵

基准组合 = 冷/热 × 短文/5000 字 × 横屏/竖屏（共 8 组），每组重复运行取 **P50 / P95**。

- 执行入口：`uv run python scripts/perf_bench.py [--runs N]`
- 结果入库：`docs/spikes/perf_bench.json`（schema `perf_bench/v1`）

## 2. 诚实口径（必须与数字一起阅读）

1. `perf_bench.py` 运行在**离线替身链路**（假 LLM / 合成 TTS / 合成图片，与
   `tests/test_e2e_smoke.py` 同一套替身），度量的是管线本身开销：
   解析、状态机、ffmpeg 渲染、QC 复核。**不含** LLM 推理、TTS、图像生成的真实延迟。
2. 因此基准数字只能用于**管线回归对比**（版本间不得显著劣化），
   不能用来向用户承诺端到端成片耗时。
3. 真实 Provider 的 P50/P95（Ollama 见 `docs/spikes/ollama_bench.json`、
   whisper 见 `docs/spikes/whisper_bench.json`）须在发布候选环境实测后回填本节，
   **不得用替身数字替代或外推**（方案待跟进事项：不造假指标）。

## 3. SLO 目标（验收口径）

| 维度 | 目标 | 状态 |
|---|---|---|
| 管线回归 | 同机同组合下，新版本的 P95 不得比基线劣化 >20% | 以最近一次入库基准为基线 |
| 端到端成片（5 分钟文案，横屏） | 待实测回填（= 管线 + LLM + TTS + 图像） | **待实测** |
| LLM 单调用 | 见 `docs/spikes/ollama_bench.json`（M0 实测） | 已入库 |
| 字幕对齐误差（<300ms 承诺） | 需人工真值集测量，未测量前按预览级口径表述 | **待真值集**（人工标注） |

## 4. 运维配额（config/ops）

| 配额 | 默认值 | 强制点 |
|---|---|---|
| `cache_max_gb` | 2.0 | `doc2video cache gc` 按 LRU 淘汰 |
| `cache_ttl_days` | 14 | 同上，超期无条件淘汰 |
| `workspace_warn_gb` | 20.0 | `cache status` 超限告警（软配额，不阻断） |
| `disk_free_min_gb` | 1.0 | `run` 启动前硬检查，不足拒绝（防写中磁盘满） |

日常维护命令：

```
doc2video cache status          # 查看缓存/工作区/磁盘用量
doc2video cache gc              # 按默认配额回收
doc2video cache gc --dry-run    # 只统计不删除
```
