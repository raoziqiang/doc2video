import {
  Callout,
  Divider,
  Grid,
  H1,
  H2,
  RadarChart,
  Row,
  Stack,
  Stat,
  Table,
  Tag,
  Text,
} from 'qoder/canvas';

const SEVERITY_TONE = {
  high: 'danger',
  medium: 'warning',
  low: 'neutral',
} as const;

const SEVERITY_LABEL = { high: '高', medium: '中', low: '低' } as const;

const findings = [
  {
    id: 'F-001',
    severity: 'high' as const,
    category: 'CI/CD',
    title: '无 CI:所有门禁依赖手工本地执行',
    recommendation:
      '新增 GitHub Actions:PR 触发 ruff + pytest + release_gate 本地层;合并后追加 --media-smoke',
  },
  {
    id: 'F-002',
    severity: 'medium' as const,
    category: '质量门禁',
    title: '测试无覆盖率度量与门禁',
    recommendation:
      '引入 pytest-cov 建立基线并纳入 release_gate;对 pipeline 模块设置最低阈值',
  },
  {
    id: 'F-003',
    severity: 'medium' as const,
    category: '质量门禁',
    title: '缺少静态类型检查门禁',
    recommendation:
      '将 opts 显式类型化,引入 pyright/mypy,从 contracts/state/pipeline 起步',
  },
  {
    id: 'F-004',
    severity: 'medium' as const,
    category: '可复现',
    title: '阶段指纹为桩级:Prompt/模型变更不会失效下游',
    recommendation:
      '指纹纳入上游产物哈希、Prompt 文件哈希、模型 digest、Schema 版本',
  },
  {
    id: 'F-005',
    severity: 'medium' as const,
    category: '可靠性',
    title: 'resume 丢失原始运行参数',
    recommendation:
      'RunOptions 持久化进 manifest/state;resume 时加载原值,允许显式覆盖',
  },
  {
    id: 'F-006',
    severity: 'low' as const,
    category: '可靠性',
    title: 'doctor 检查执行两次,副作用翻倍',
    recommendation: 'checks = _doctor_checks(cfg) 仅调用一次,循环与汇总共用',
  },
  {
    id: 'F-007',
    severity: 'low' as const,
    category: '可靠性',
    title: 'EventLog seq 依赖全量读,单写者未强制',
    recommendation: '维护末尾 seq 游标;文档化单写者契约或类内持锁',
  },
  {
    id: 'F-008',
    severity: 'low' as const,
    category: '质量门禁',
    title: 'CJK 字体路径硬编码为 Windows 路径',
    recommendation: '字体解析抽为平台适配函数;非目标平台输出 skipped 而非 warn',
  },
];

const strengths = [
  ['崩溃安全的编排内核', '原子写盘 + 跨进程锁 + 状态机转移表 + resume 全量重验级联失效'],
  ['契约与 Schema 漂移治理', '16 份 Schema 生成并逐字节比对,漂移即 fail;LLM 输出仅有界语法修复'],
  ['Fail-closed 分层发布门禁', '本地 → 30s 媒体 smoke → live 三层;凭据缺失输出 blocked,不伪造通过'],
  ['带校验和的事件审计', 'events.jsonl 每行 seq+SHA-256 校验和,读取丢弃半行/损坏行'],
  ['缓存校验与凭据隔离', '缓存命中强制 SHA-256;密钥不进配置指纹;报告输出全量 redact'],
  ['证据驱动的配置冻结', 'M0 Spike 实测值 + 模型 digest 回填配置,变更可追溯'],
  ['预算与隐私硬护栏', '云调用/成本上限、重试上限;默认 offline,云调用需显式开关'],
];

const dimensionRows = [
  { dimension: '可靠性/崩溃安全', current: 9.0, target: 9.5 },
  { dimension: '可观测与审计', current: 9.0, target: 9.0 },
  { dimension: '安全与凭据', current: 9.0, target: 9.0 },
  { dimension: '测试自动化', current: 8.5, target: 9.5 },
  { dimension: '质量门禁', current: 7.5, target: 9.0 },
  { dimension: '确定性/可复现', current: 7.0, target: 9.0 },
  { dimension: 'CI/CD', current: 2.0, target: 8.0 },
];

export default function BetterHarnessReport() {
  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>doc2video Harness 实践洞察</H1>
        <Text tone="secondary">
          生成于 2026-08-28 · 项目版本 0.1.0 · 成熟度:advanced · 综合 7.8 / 10
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="7.8 / 10" label="综合得分" tone="info" />
        <Stat value="156 通过" label="测试(24.75s)" tone="success" />
        <Stat value="8" label="保留问题" tone="warning" />
        <Stat value="1" label="高严重度" tone="danger" />
      </Grid>

      <Callout tone="info" title="结论">
        内核级可靠性与发布门禁实践成熟(原子提交、状态机、契约漂移检测、fail-closed
        凭据处理),156 项测试全绿;主要短板在门禁未接入 CI、无覆盖率/静态类型门禁、阶段指纹仍为桩级。
      </Callout>

      <Divider />

      <H2>能力画像(当前 → 目标)</H2>
      <RadarChart
        data={dimensionRows}
        dataKeys={['current', 'target']}
        angleKey="dimension"
        maxValue={10}
        height={340}
        valueSuffix=" /10"
        ariaLabel="doc2video Harness 七维能力雷达图"
      />

      <H2>亮点实践</H2>
      <Table
        headers={['实践', '说明']}
        rows={strengths}
      />

      <H2>保留问题与建议</H2>
      <Table
        headers={['编号', '严重度', '类别', '问题', '建议']}
        rows={findings.map((f) => [
          f.id,
          <Tag key={f.id} tone={SEVERITY_TONE[f.severity]}>
            {SEVERITY_LABEL[f.severity]}
          </Tag>,
          f.category,
          f.title,
          f.recommendation,
        ])}
        rowTone={findings.map((f) =>
          f.severity === 'high'
            ? ('danger' as const)
            : f.severity === 'medium'
              ? ('warning' as const)
              : ('default' as const),
        )}
      />

      <H2>证据来源</H2>
      <Stack gap={4}>
        <Text size="small">pytest 实跑:156 passed in 24.75s(2026-08-28)</Text>
        <Text size="small">
          docs/release/release_gate.json:最近 --live 报告 12/13
          pass;FAL_KEY 缺失 → blocked(未伪造通过)
        </Text>
        <Row gap={8} wrap>
          {['scripts/release_gate.py', 'src/doc2video/state.py', 'src/doc2video/pipeline/runner.py', 'src/doc2video/cli.py', 'src/doc2video/cache.py', 'config/default.yaml'].map(
            (file) => (
              <Tag key={file} tone="neutral">
                {file}
              </Tag>
            ),
          )}
        </Row>
      </Stack>
    </Stack>
  );
}
