# Skill-MAS 单数据集复用代码

这是 Skill-MAS 的可移植版本，用于在一台新电脑上针对一个 benchmark 运行训练、进化和测试。仓库不包含 benchmark 原始数据、API 密钥、运行结果或日志。

## 支持的数据集适配器

- `gaia`
- `locobench_fi`
- `locobench_cr`
- `loca`
- `beyondswe_depmigrate`
- `beyondswe_crossrepo`

每次命令只接收一个 `--bench-backend`，因此一次运行只对应一个数据集适配器。

## 安装

需要 Python 3.11 或更高版本：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

如果 benchmark 自己需要额外的依赖，请按其官方说明安装到同一个环境中。

## API 配置

复制环境模板并填写自己的配置：

```bash
cp .env.example .env
source .env
```

至少设置：

```bash
export SKILL_MAS_API_KEY='your-api-key'
export SKILL_MAS_BASE_URL='https://your-openai-compatible-endpoint/v1'
export SKILL_MAS_AGENT_LLM='gpt-5.6-luna'
```

`skill_mas/model_config.json` 只保存模型名称、价格和环境变量占位符，不保存真实 endpoint 或密钥。也可以运行脚本时指定 `SKILL_MAS_ENV_FILE=/path/to/provider.env`。

## 外部数据路径

benchmark 数据不随 Git 上传。根据适配器设置对应环境变量：

```bash
export SKILL_MAS_GAIA_ROOT=/path/to/gaia
export SKILL_MAS_LOCOBENCH_ROOT=/path/to/locobench
export SKILL_MAS_LOCA_ROOT=/path/to/loca
export SKILL_MAS_BEYONDSWE_ROOT=/path/to/beyondswe
```

数据格式和默认目录约定见 [`datasets/README.md`](datasets/README.md)。也可用 CLI 的 `--jsonl` 或适配器专用参数传入数据文件。

## 查看数据集任务

```bash
./Skill_MAS/run_single_training.sh list-val \
  --bench-backend gaia \
  --max-problems 20
```

## 训练和进化

下面的例子使用 GAIA；替换 `--bench-backend` 和对应数据路径即可用于其他单数据集：

```bash
./Skill_MAS/run_single_training.sh evolve \
  --bench-id gaia \
  --bench-backend gaia \
  --domain general \
  --rounds 1 \
  --k-trajectories 2 \
  --max-concurrency 5 \
  --agent-llm "$SKILL_MAS_AGENT_LLM" \
  --user-llm "$SKILL_MAS_AGENT_LLM" \
  --evaluator-llm "$SKILL_MAS_AGENT_LLM" \
  --optimizer-llm "$SKILL_MAS_AGENT_LLM"
```

`k-trajectories >= 2` 才会产生用于对比反思的多条轨迹；`k=1` 可以运行单条基线轨迹，但不会触发这一步进化。

训练结果、轨迹 checkpoint 和日志默认写入 `Skill_MAS/results/`、`Skill_MAS/logs/` 和 `Skill_MAS/runs/`。这些目录被 `.gitignore` 忽略。

## 固定技能测试

指定已有的技能目录运行 native 测试：

```bash
./Skill_MAS/run_single_training.sh native-test \
  --bench-backend gaia \
  --bench-id gaia \
  --frozen-skill /path/to/skills/SKILL.md \
  --run-id fixed_test \
  --max-concurrency 5
```

CLI 帮助可以查看完整参数：

```bash
PYTHONPATH="$PWD" python3 -m Skill_MAS.core.cli --help
PYTHONPATH="$PWD" python3 -m Skill_MAS.core.cli native-test --help
```

## 恢复和超时

运行会保存任务轨迹和 checkpoint。再次使用相同的 `--run-id` 可以从已有状态继续。native 任务把有效执行时间与 API 恢复等待分开计算：API 恢复使用独立的 800 秒墙钟窗口，不计入有效任务预算；超时任务的轨迹会保留在结果中，便于诊断。

## 发布前检查

上传前确认没有数据、密钥、个人路径和结果：

```bash
rg -n 'sk-|api[_-]?key|Users/|mxdapi|OPENAI_API_KEY|OPENAI_API_BASE' .
git status --short
```

只提交源代码、配置模板和文档，不提交 `.env`、benchmark 目录、`results/`、`logs/` 或 `runs/`。
