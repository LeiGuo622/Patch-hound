# PatchHound OMP expert

运行安装、认证、配置和输入结构见项目根目录 [README](../../README.md)。

```bash
# 从项目根目录执行，无模型调用：
uv run python src/experiments-omp-expert/run_case.py case_0001 --dry-run

# 安装并认证后实际运行自制示例：
uv run python src/experiments-omp-expert/run_case.py case_0001 --no-history-tool --no-phoenix

# 查看批处理和配置选项：
uv run python src/experiments-omp-expert/run_cases_batch.py --help
uv run python src/experiments-omp-expert/run_case.py --help
```

保留运行源码、提示词、沙箱脚本、配置、测试和依赖清单；运行结果不入库。
历史 MCP 使用根目录 `tools/history_mcp_server.py`。API 配置中的示例端点需要替换为自己的服务地址。

## 运行配置

仅保留以下四份模型运行配置：

| 用途 | 文件 |
|---|---|
| 默认 GPT-5.6-sol | `omp_expert_runtime.toml` |
| NewAPI DeepSeek V4 Flash | `omp_expert_runtime.newapi-deepseek-v4-flash.toml` |
| DGX DeepSeek V4 Flash 0731 | `omp_expert_runtime.dgx-v4-flash-0731.toml` |
| NewAPI GLM-5.3 | `omp_expert_runtime.newapi-glm-5.3.toml` |
