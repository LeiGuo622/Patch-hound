# PatchHound

PatchHound 通过隔离的源码访问和补丁审查流程，分析代码变更是否引入安全问题。
本目录包含 Codex、OMP、OpenCode 的 simple/expert 六个 agent，以及运行所需的程序、提示词、配置和测试。
仅附带一个自制教学 case，不包含真实实验数据、评测结果、报告、凭据或运行记录。

## 六种 agent

| 实现 | 单次运行入口 |
|---|---|
| Codex simple | `src/experiments-codex-simple/run_case.py` |
| Codex expert | `src/experiments-codex-expert/run_case.py` |
| OMP simple | `src/experiments-omp-simple/run_case.py` |
| OMP expert | `src/experiments-omp-expert/run_case.py` |
| OpenCode simple | `src/experiments-opencode-simple/run_case.py` |
| OpenCode expert | `src/experiments-opencode-expert/run_case.py` |

每个目录还提供 `run_cases_batch.py`、隔离探针和历史工具探针。
Simple 直接审查补丁；expert 增加知识选择、阶段规划和分阶段分析。

## 安装

运行环境为 Linux、Python 3.13+ 和 uv。实际模型运行还需要相应账号或 API 凭据，以及 bubblewrap 或受支持的 Landlock 环境。

```bash
uv sync
cp .env.example .env
```

Codex agent 使用单独安装的 Python SDK，提交固定在 `requirements-codex.txt`：

```bash
uv pip install -r requirements-codex.txt
codex login
```

使用与该 SDK 匹配的 Codex CLI；可通过各入口的 `--codex-bin` 指定可执行文件。
程序直接导入安装的 `openai_codex` 包，不依赖 `third_party/codex` 或上游示例 bootstrap。

OMP 需要 Bun，各目录安装并构建自己的 sidecar：

```bash
cd src/experiments-omp-simple
bun install --frozen-lockfile
bun run build
```

Expert 使用 `src/experiments-omp-expert` 执行相同命令。账号认证使用对应 OMP 安装的登录流程。

OpenCode 需要 Node.js 22+，各目录安装并构建自己的 sidecar：

```bash
cd src/experiments-opencode-simple
npm ci
npm run build
```

Expert 使用 `src/experiments-opencode-expert` 执行相同命令。账号认证使用对应 OpenCode 安装的登录流程。
四个 sidecar 实现各自的 `pyproject.toml` / `uv.lock` 还提供独立 Python 环境和开发测试依赖。

## 检查和运行自制示例

先在项目根目录预览输入；不会启动模型、历史工具或 Phoenix，也不会分配运行目录：

```bash
uv run python src/experiments-codex-simple/run_case.py case_0001 --dry-run
uv run python src/experiments-codex-expert/run_case.py case_0001 --dry-run
uv run python src/experiments-omp-simple/run_case.py case_0001 --dry-run
uv run python src/experiments-omp-expert/run_case.py case_0001 --dry-run
uv run python src/experiments-opencode-simple/run_case.py case_0001 --dry-run
uv run python src/experiments-opencode-expert/run_case.py case_0001 --dry-run
```

安装、认证完成后，实际调用模型审查该示例：

```bash
uv run python src/experiments-codex-simple/run_case.py case_0001 --no-history-tool --no-phoenix
```

其余五种实现替换入口目录即可，均使用 `--no-history-tool --no-phoenix`。
示例源码是补丁后的版本，内容完全自制；不提供真实提交元数据。关闭历史查询的运行不属于带历史工具的实验配置。
输出会生成在 case 的相应 `*-runs/` 目录；输出和新加入的真实 case 默认被 Git 忽略。

## 准备自己的 case

每个 case 使用下列结构：

```text
cases/case_XXXX/
  input/commit_message.txt
  input/diff.txt
  source/                  # 补丁后的源码，不含 .git
```

也可从自己提供的提交 CSV（列名 `commit_id`）和外部 Linux checkout 准备输入。
仓库提供 [CSV 示例模板](examples/commits.example.csv)，其中两行提交值均为占位符，不包含真实实验提交。

先复制模板：

```bash
cp examples/commits.example.csv commits.local.csv
```

将 `commits.local.csv` 中的占位符替换为本地 Linux 仓库中的目标提交 SHA，可按需增删数据行。
填写后运行：

```bash
uv run python -m tools.prepare_cases commits.local.csv --linux-repo /path/to/linux
```

`*.local.csv` 默认被 Git 忽略，填写后的真实提交列表不会随普通提交上传。
准备程序同时生成仅供宿主历史工具使用的 `cases/case_metadata/`。该目录及 Linux 仓库不分发，也不提供给模型的源码 shell。
历史工具位于根目录 `tools/`，按 `LINUX_REPO_PATH` 或 `--history-linux-repo` 使用外部源码仓库。

## 运行配置

每个 agent 统一保留四份 runtime TOML：默认 GPT-5.6-sol、NewAPI DeepSeek V4 Flash、DGX DeepSeek V4 Flash 0731、NewAPI GLM-5.3。具体文件名见各 agent 的 README。账号认证是默认配置；API provider 使用 `.env` 中对应的 key。
原实验中转站和内网主机地址已替换为占位地址：

- OMP/OpenCode 可在 `.env` 设置 `PATCHHOUND_NEWAPI_BASE_URL`、`PATCHHOUND_DGX_BASE_URL` 或 `PATCHHOUND_GLM_BASE_URL`。
- Codex API 配置需复制成 `*.local.toml`，填写自己的 `model_providers` 下的 `base_url`，通过 `--codex-runtime-config` 选择。
- 各入口的 `--help` 列出该实现可用的配置、超时、模型和输出选项。

Expert 默认从 `third_party/sashiko/third_party/prompts/kernel` 读取 guidance。
该路径已包含固定版本的内核提示词最小快照，无需安装完整 Sashiko 或初始化 submodule。
来源、版本、许可证、本地修改和可选获取方法见 [third_party/README.md](third_party/README.md)。
`third_party/review-prompts/` 和 `third_party/patchhound-review-knowledge/` 仅保留空目录占位。

Phoenix 为可选功能，部署配置见 `deploy/phoenix/compose.yaml`；运行时用 `--no-phoenix` 关闭。

## 测试

Codex 实现使用 unittest：

```bash
uv run python -m unittest discover -s src/experiments-codex-simple/tests
uv run python -m unittest discover -s src/experiments-codex-expert/tests
uv run python -m unittest discover -s tests
```

OMP/OpenCode 实现可在各自目录用 `uv sync --group dev` 后执行 `uv run pytest`，TypeScript 测试使用各目录的 package scripts。
不支持用户命名空间的宿主会跳过相应实时 bubblewrap 探针；其他测试使用自建临时输入或 fake runtime。
