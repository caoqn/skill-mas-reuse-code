# MIX-COOP 混合训练复现实验协议

更新日期：2026-09-23

本文是 MIX-COOP 混合训练的复现入口。它记录固定的实验设计、数据划分、随机种子、初始池、预算、运行命令、冻结团队和审计要求。API 密钥不写入本文；运行者只需准备与本文路径相同的本地 `.env` 文件。

## 1. 实验对象

MIX-COOP 将四个原生 benchmark 放入同一条共享演化链：GAIA、LoCoBench、LOCA-Bench 和 BeyondSWE。每个 benchmark 仍由自己的 adapter 负责任务构造、工具/环境、输出合约和 evaluator；共享的是团队演化状态：family/template、Agent prompt/skill、Chairman profile 和 family-scoped handoff rules。

运行流程为：原生 adapter 构造任务 → family selector 复用 family 或 cold start → Chairman 从候选 roster 招募实际成员 → Agent 执行并按需交接 → 原生 evaluator 评分 → 反思并持久化下一版团队。

## 2. 固定软件与目录

源码根目录：

```text
/Users/caoqinuo/Desktop/6月课题/meta_team evolution/new code
```

关键入口：

```text
benchmarks/MIX-COOP/mixed_scheduler.py       # MIX 历史/对照入口
benchmarks/test_group/mixed_scheduler.py     # 机制实验组入口（如使用）
benchmarks/MIX-COOP/manifests/               # 物化任务顺序
agents/pool_MIX_COOP/                        # 共享初始池
agents/*v116*frozen/                         # 只读冻结池
core/runner.py                               # Chairman、执行、超时
core/handoff_reflector.py                    # handoff 反思
core/team_selector.py                        # family 选择/cold start
core/run_manager.py                          # case 与 vNNN 版本链
apiconfig/                                   # 私有 API 配置
```

## 2.1 从零开始安装

下面的步骤假设一台干净的 Linux/macOS 机器，推荐 Python 3.11、Git、Docker Engine 以及至少 100 GB 可用磁盘空间。LOCA-Bench 和 BeyondSWE 的完整数据/镜像会显著增加磁盘需求。

```bash
git clone <本项目仓库地址> "meta_team evolution/new code"
cd "meta_team evolution/new code"
python3.11 -m venv .venv311
source .venv311/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt
```

如果当前仓库没有 `requirements.txt`，按本地代码实际依赖安装并记录版本：

```bash
python -m pip install pyyaml python-dotenv requests pydantic tenacity tqdm
python -m pip freeze > reproduction_requirements.lock.txt
```

安装后先确认解释器和代码可导入：

```bash
python --version
python -c "import yaml, requests; print('python dependencies: OK')"
python -m pytest -q tests/test_mix_coop_manifest.py tests/test_mix_coop_runtime_privacy.py
```

不要提交 `.venv*`、API env 文件、数据集密钥或 Docker registry 凭据。

## 2.2 Docker 与系统服务

BeyondSWE 需要 Docker 仓库容器；LOCA-Bench 需要其 MCP/本地状态服务。安装 Docker 后验证：

```bash
docker version
docker info
docker run --rm hello-world
```

若 `docker info` 失败，先修复 daemon、用户组权限或 Docker Desktop，再开始训练。Docker/API/MCP 初始化失败属于基础设施错误，不能当作模型失败写入演化链。

BeyondSWE 所需镜像和仓库数据由 `benchmarks/adapter_beyondswe.py` 的数据配置决定；不要自行把任意镜像标签替换成 `latest`。首次运行前先用一题 smoke 检查能否拉取并启动容器：

```bash
python benchmarks/adapter_beyondswe.py --help
# 然后按该 adapter 的单题 smoke 参数运行一题，并检查 result.json
```

若仓库没有该验证脚本，使用正式 adapter 的 `--dry-run`/单题命令，并确认 `result.json` 中没有 `infrastructure_failure=true`。

## 3. 数据获取与目录布局

数据不通过 API key 自动生成；复现实验必须使用固定版本的数据快照。建议把数据放在源码目录之外，再通过环境变量或项目约定的 `data/` 软链接接入：

```text
<project-root>/data/
├── gaia/
├── locobench/
├── locabench/
└── beyondswe/
```

推荐下载顺序：先获取官方原始数据，再运行本项目的 split/manifest 生成脚本，最后执行 native resolver 校验。不要从运行结果中的 `task_id` 反推或手工拼接题目。

### GAIA

GAIA 使用 Hugging Face GAIA 2023 snapshot（题目 metadata/parquet 与附件文件）以及本项目固定的 20 题训练划分。需要准备：

```text
data/gaia/hf_snapshot/2023/validation/
data/gaia/hf_snapshot/2023/test/
```

官方数据通常需要 Hugging Face 访问权限/登录；按官方许可证和仓库说明下载完整 `validation`、`test` snapshot。下载后检查每个 split 同时存在 metadata parquet 和附件目录：

```bash
test -f data/gaia/hf_snapshot/2023/validation/metadata.parquet
test -f data/gaia/hf_snapshot/2023/test/metadata.parquet
python - <<'PY'
import pandas as pd
for p in ('data/gaia/hf_snapshot/2023/validation/metadata.parquet',
          'data/gaia/hf_snapshot/2023/test/metadata.parquet'):
    print(p, len(pd.read_parquet(p)))
PY
```

本项目训练/测试使用的 GAIA 划分索引位于 adapter 和 manifest 中；不要用最新在线数据替代固定 snapshot，否则附件、答案和 task ID 可能变化。

### LoCoBench

LoCoBench 的 Python FI/CR 任务需要官方代码任务数据及每题的 context/solution 工作区。准备后应能由 `benchmarks/adapter_locobench.py` 按 task id 定位样本。建议目录形态：

```text
data/locobench/
├── python/
│   ├── feature_implementation/
│   └── cross_file_refactoring/
└── metadata.*
```

通过 LoCoBench 官方发布页/仓库获取对应版本；本项目的 20 题 evolve 和 80 题 FI/CR holdout 划分沿用 adapter 中的 paper split。下载完成后用以下命令验证 native 定位：

```bash
python benchmarks/validate_manifest.py \
  benchmarks/MIX-COOP/manifests/mix_coop_official_116_seed20260828.json \
  --resolve-native
```

### LOCA-Bench

LOCA-Bench 同时需要任务定义、MCP 服务和隔离的 local state。准备官方 LOCA-Bench 数据/环境包后，目录应至少包含：

```text
data/locabench/
├── gem/envs/
├── tasks/                 # 若发布包包含任务索引
└── seeds/                 # 原生 seed 配置（101、102、以及测试 seeds）
```

安装其官方服务依赖并确认 `loca_mcp` 工具可以启动。MIX-COOP 训练只使用 `evolve_96k` 的 16 个样本；正式 holdout 为 8 tasks × 5 seeds × 6 context scales = 240。不要把 96K evolve 题重复计入 holdout。

### BeyondSWE

BeyondSWE 的 CrossRepo 和 DepMigrate 任务需要官方任务索引、仓库 patch 基线、测试命令和 Docker 镜像。推荐目录形态：

```text
data/beyondswe/
├── crossrepo/
├── depmigrate/
├── repositories/
└── metadata.*
```

按 BeyondSWE 官方 release/仓库获取与 adapter 兼容的固定版本。训练使用两个 track 各自的 0–19；测试使用 CrossRepo 20–199（180）和 DepMigrate 20–177（158）。每个 Docker case 必须从干净仓库启动，禁止复用上一个 case 的容器或工作区。

## 3.1 数据版本与校验清单

在开始正式训练前，把以下信息写入实验记录：数据来源 URL/release、下载日期、commit/tag、文件数量、总字节数和 SHA-256 manifest。示例：

```bash
find data/gaia data/locobench data/locabench data/beyondswe -type f -print0 \
  | sort -z \
  | xargs -0 shasum -a 256 > data_checksums.sha256
```

然后执行：

```bash
python benchmarks/MIX-COOP/validate_manifest.py \
  benchmarks/MIX-COOP/manifests/mix_coop_official_116_seed20260828.json \
  --resolve-native
```

校验必须确认：116 个 evolve task 全部可解析；四个 benchmark 的 task id、split、source index 与 manifest 一致；没有缺失附件、仓库、MCP seed 或 evaluator 资源。解析失败时停止，不要用空任务或临时替代样本继续训练。

使用项目自己的 Python 环境：

```bash
cd "/Users/caoqinuo/Desktop/6月课题/meta_team evolution/new code"
.venv311/bin/python ...
```

## 3. 正式 116 题训练集

正式 manifest：

```text
benchmarks/MIX-COOP/manifests/mix_coop_official_116_seed20260828.json
```

固定数据构成：

| 来源 | split/范围 | 数量 |
|---|---|---:|
| GAIA | `train_20` | 20 |
| LoCoBench | Python Feature Implementation | 20 |
| LoCoBench | Python Cross-file Refactoring | 20 |
| LOCA-Bench | `evolve_96k`，8 tasks × seeds 101/102 | 16 |
| BeyondSWE | CrossRepo 0–19 | 20 |
| BeyondSWE | DepMigrate 0–19 | 20 |
| **合计** |  | **116** |

### 随机种子与顺序

- `ordering.strategy`: `global_shuffle`
- `ordering.seed`: **20260828**
- `round_unit`: one task per source benchmark
- `shuffle_within_round`: `true`
- `shuffle_scope`: `evolve`
- `materialized_order`: `true`
- `order_hash`: `a1776e9e06961a3d545c9b628e9b2015acb701779367080bd2d8801e589adc15`

这里的 seed 只用于生成训练任务的固定顺序；manifest 已经物化，因此复现时必须直接读取该 manifest，不能重新随机抽样或重新 shuffle。`mix_coop_official_116_seed20260908.json` 是另一份 seed=20260908 的顺序，不能与本实验混用。

## 4. 共享池与演化规则

初始池：

```text
agents/pool_MIX_COOP
```

主席：`planner`。共享执行角色包括 `researcher`、`context_analyst`、`implementer`、`integrator`、`verifier` 和 `answer_agent`（answer agent 为最终答案协议服务，不作为普通 family 成员）。所有 116 题训练使用同一目录和同一条版本链 `v000 -> v001 -> ...`；不能把已演化目录直接作为另一组的 `v000`。

`pool.yaml` 当前重要设置：

```yaml
max_seconds: 1800
effective_task_seconds: 1200
wall_clock_seconds: 3000
max_messages: 500
reflection_phase_timeout: 600
team_selection_enabled: true
chairman_teammate_profiles_enabled: true
member_teammate_profiles_enabled: false
```

只有 Chairman 读取/更新成员画像。画像只记录实际任务观察，不构成招募优先级建议；执行成员不读取或反思 profile，依赖自身 skill 和 handoff rule 协作。

## 5. MIX-COOP 两个机制条件

若只复现正式历史 MIX 训练，使用对照入口：

```text
benchmarks/MIX-COOP/mixed_scheduler.py
```

该路径保持 MIX 历史行为：超时 handoff trace 不用于反思，handoff 反思按历史交互批次处理。

若复现机制实验组，使用：

```text
benchmarks/test_group/mixed_scheduler.py
```

该路径与对照使用相同 manifest、模型、预算和 native evaluator，但启用按有向 Agent pair 聚合的 handoff 反思、超时轨迹保留，以及训练期 `menu -> inspect -> send_message` 两阶段规则选择。两组必须从内容一致但目录独立的 `v000` 开始，不能共享在线写入目录。

## 6. 时间预算、模型与 API

模型固定为：`gpt-5.6-luna`。训练调度固定 `workers=1`，因为共享演化状态必须串行更新；并行只适用于冻结池的测试。

混合调度器采用各 native adapter 的默认预算：

| Benchmark | task/effective timeout | wall clock | 备注 |
|---|---:|---:|---|
| GAIA | 900s | 3000s | 原生短答案 |
| LoCoBench | 1800s | 2400s | Python solution workspace |
| LOCA-Bench `evolve_96k` | 7200s | 7200s | MCP/stateful environment |
| BeyondSWE evolve | 2400s | 2400s | Docker repository |

API 单题默认最多重试 3 次、重试间隔 30s；连续 3 次基础设施失败时 circuit cooldown 为 120s。API 配置只通过环境文件注入，例如：

```bash
set -a
source apiconfig/gaia.env
set +a
```

实际复现时必须记录使用的文件名和 endpoint，但不得把 `OPENAI_API_KEY` 或其他密钥提交到仓库。

## 7. 训练命令

先校验 manifest（如果 native 数据路径已配置）：

```bash
.venv311/bin/python benchmarks/MIX-COOP/validate_manifest.py \
  benchmarks/MIX-COOP/manifests/mix_coop_official_116_seed20260828.json \
  --resolve-native
```

运行对照训练：

```bash
set -a; source apiconfig/gaia.env; set +a
.venv311/bin/python benchmarks/MIX-COOP/mixed_scheduler.py \
  --manifest benchmarks/MIX-COOP/manifests/mix_coop_official_116_seed20260828.json \
  --phase evolve \
  --team pool_MIX_COOP \
  --run-id mixcoop_control_seed20260828_r1
```

运行机制实验组训练：

```bash
set -a; source apiconfig/gaia.env; set +a
.venv311/bin/python benchmarks/test_group/mixed_scheduler.py \
  --manifest benchmarks/MIX-COOP/manifests/mix_coop_official_116_seed20260828.json \
  --phase evolve \
  --team pool_MIX_COOP \
  --run-id mixcoop_treatment_seed20260828_r1
```

若进程中断，使用同一 `run-id` 加 `--resume`。只有有效且非基础设施失败的 case 才能推进版本链；不要跳过污染样本，也不要覆盖旧 run。

## 8. 冻结与正式测试

训练完成后，冻结最后一个有效版本（例如 v116）：

```bash
bash scripts/freeze_evolved_team.sh \
  <evolve_run_id> latest \
  <frozen_pool_name>
```

仓库中已存在的参考冻结池：

```text
agents/pool_MIX_COOP_official116_seed20260828_v116_frozen
agents/pool_MIX_COOP_test_group_split_seed20260828_v116_frozen
```

它们的来源分别记录在各自 `FROZEN_FROM.md` 中。冻结池必须只读，测试不加 `--evolve`，不产生新的 team version。

正式 holdout（不包括 manifest 内仅用于协议检查的 8 个 test smoke）：

| Benchmark/track | holdout 数量 |
|---|---:|
| GAIA `test_100` | 100 |
| LoCoBench FI | 80 |
| LoCoBench CR | 80 |
| LOCA-Bench 6 context scales | 240 |
| BeyondSWE CrossRepo | 180 |
| BeyondSWE DepMigrate | 158 |
| **合计** | **838** |

当前 official 116 manifest 的 test 部分只有四个 benchmark 各 2 个样本，共 8 个协议 smoke；它不能替代上述 838 题正式测试。正式论文测试应先物化并校验完整 holdout manifest，记录其 seed、顺序和 hash，再通过对应 MIX scheduler 的 `--phase test` 执行。

## 9. 运行产物与可复现审计

每个 run 至少应保留：

```text
runs/<run_id>/config.json
runs/<run_id>/cases/*/session.json
runs/<run_id>/cases/*/events.jsonl
runs/<run_id>/cases/*/result.json
runs/<run_id>/team/vNNN/
runs/<run_id>/summary.json
```

`config.json` 必须能追溯 run-id、manifest/suite、seed/order hash、pool、模型、预算和 orchestration mode。完成后检查：

```bash
ps -axo pid,etime,%cpu,stat,command | rg 'mixed_scheduler.py'
find runs/<run_id>/cases -name result.json | wc -l
rg -n 'INFRASTRUCTURE_FAILURE|API_FAILURE|circuit open|Connection error' \
  runs/<run_id>/cases/*/{result.json,events.jsonl}
```

最终报告至少包括：每个 benchmark 的 native 分数、有效样本数、基础设施失败/重跑数、冻结 pool 与来源 vNNN、family/template 数量、active handoff rule 数量、rule inspect/use 次数、cold-start/reuse 次数和实际招募分布。不同 benchmark 的 native raw score 不直接相加或简单平均；应分别报告，再定义明确的跨套件聚合指标。

## 10. 已验证参考记录

参考对照训练：

```text
run_id: 20260904_mixcoop_official116_seed20260828_r1
manifest: mix-coop-official-116-seed20260828
team: pool_MIX_COOP
records: 116
versions: v000 -> v116
summary: runs/20260904_mixcoop_official116_seed20260828_r1/summary.json
```

该记录的有效训练汇总为：GAIA 20、LoCoBench 40、LOCA-Bench 16、BeyondSWE 40；基础设施失败 0。其 `summary.json` 仅是历史运行结果，不应替代新的复现实验。
