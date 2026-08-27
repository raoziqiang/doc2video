# M0 Spike 报告(2026-08-27)

> 结论数据见各 `docs/spikes/*.json`。本报告为人工可读摘要 + 冻结决策记录。

## 1. ffmpeg(6/6 通过 ×3 连跑)

| 验证项 | 结果 |
|--------|------|
| C.1 单场景 Ken Burns + 数值 fade + adelay(16:9) | ✅ H.264 High 1920×1080 yuv420p / AAC 48kHz 双声道 / 3.52s |
| C.1 同命令竖屏 9:16 | ✅ 1080×1920 同规格 |
| C.2 concat(统一流参数) | ✅ 7.06s |
| C.3 sidechaincompress 方向(BGM=main,旁白=sidechain) | ✅ 增益实测:无 duck −27.1dB → 正确方向 −32.2dB(−5.1dB);反接对照 −27.1dB(不压缩 BGM) |
| 两遍 loudnorm(measure→apply) | ✅ measured_* 参数回读成功 |
| C.4 ASS 烧录 + CJK | ✅ fontconfig 自动解析 Microsoft YaHei |

**实测坑(写入 M4 开发注意):**

1. **crop 滤镜不接受 `WxH` 字母 x 语法**——`crop=3840x2160` 报 `Invalid chars 'x2160' at the end of expression`;必须用冒号 `crop=3840:2160`。scale 与 zoompan 的 `s=` 接受 WxH。
2. **滤镜参数里的 Windows 盘符冒号无法转义**——`fontsdir=C:/Windows/Fonts` 报 `No option name near '/Windows/Fonts'`(`\:` 也无效)。对策:①该 build 带 fontconfig,`ass=文件` 不传 fontsdir 即可自动解析系统字体;②fontsdir 用无冒号相对路径;③ffmpeg 子进程 cwd 设为素材目录,滤镜参数用相对路径。
3. 时间参数必须是**纯数值**(st=/d= 不接受算式字符串)——本方案 v0.1 复审教训,本次全部数值验证通过。

## 2. Ollama(9/9 通过)

| 冻结项 | 值 |
|--------|-----|
| 版本 | 0.33.1 |
| 模型 | `qwen3-14b-agent:latest`(digest `02dc9bf0…dd82`;本机另有 Qwen3:14b / qwen3-8b-long / qwen2.5vl:3b) |
| 结构化输出 | `format=json-schema`(原生 /api/chat)实测可用:2 场景 24.9s(含冷启动),content 247 字符非空 |
| think:false | ✅ 空 content 坑未复现(原生 API 路径) |
| 并发 | 2 并发请求实测有重叠;应用侧保守并发 1,doctor 报告实际值 |
| num_ctx | 8192(请求显式传入) |

## 3. faster-whisper(5/5 通过)

- **CUDA 不可用**:`cublas64_12.dll` 缺失(无 CUDA 12 运行库)。**声明 CPU 路径**:small @ cpu int8 实测 **RTF 0.17x**(9.8s 音频 1.7s),中文覆盖率 100%,词级时间戳 33 words。
- **字幕方案冻结**:主路径 = edge-tts 原生 WordBoundary/SentenceBoundary marks;aligner = faster-whisper word_timestamps + 字符比例映射(确定性、零新依赖);whisperX 不引入(torch 重依赖);stable-ts 列为 M3 可选增强;ASR 回读仅做覆盖 QC 与低置信兜底。

## 4. FAL(契约冻结;直连 smoke blocked)

- **端点冻结**:`fal-ai/flux-2/klein/4b/base`,队列 `https://queue.fal.run/fal-ai/flux-2/klein/4b/base`,输入/输出 Schema 快照见 `fal_contract_snapshot.json`(2026-08-27 官方文档核验)。
- **直连 smoke:blocked**——项目 .env 无 FAL_KEY。**模型级功能验证**:FLUX 2 Klein 经 Hermes 网关实测出图成功(风格测试图)。
- **解锁方式**:在 `.env` 配置 `FAL_KEY` 后重跑 `scripts/spikes/fal_smoke.py` 即完成真实 contract smoke;首次计费后回填单价。

## 5. M0 结论

- 文本级 P0 全部关闭(契约/状态机/CLI/空管线:71 测试全绿);
- 三审前仍需:① FAL 直连真实 smoke(需 key);② 真实发布 smoke gate(M7,依赖 M1–M5 产物);
- 已提交资产:16 类 Schema、契约模型、状态机原语、CLI、doctor、4 组 Spike 脚本与结果 JSON、冻结后的 config。
