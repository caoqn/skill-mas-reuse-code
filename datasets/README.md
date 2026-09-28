# 外部数据集

本目录只提供说明，不存放 benchmark 原始数据。请从各项目的官方来源获取数据，并通过环境变量告诉 Skill-MAS 数据位置。

| 适配器 | 环境变量 | 备注 |
|---|---|---|
| `gaia` | `SKILL_MAS_GAIA_ROOT` | GAIA 数据集根目录 |
| `locobench_fi` / `locobench_cr` | `SKILL_MAS_LOCOBENCH_ROOT` | LoCoBench 官方目录 |
| `loca` | `SKILL_MAS_LOCA_ROOT` | LOCA 官方目录 |
| `beyondswe_depmigrate` / `beyondswe_crossrepo` | `SKILL_MAS_BEYONDSWE_ROOT` | BeyondSWE 官方目录 |

如果使用自定义 JSONL 文件，可在 CLI 中传入 `--jsonl /path/to/tasks.jsonl`。不要把这些文件复制到本仓库后提交。
