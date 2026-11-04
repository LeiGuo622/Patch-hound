# 第三方依赖与提示词来源

本项目通过包管理器安装 SDK，并随仓库提供 expert 运行所需的固定版本内核提示词快照。
提示词已经包含在下列目录中，克隆本项目后无需下载完整 Sashiko 工程或初始化 submodule。

```text
third_party/
├── README.md
├── sashiko/
│   ├── LICENSE
│   └── third_party/prompts/
│       ├── LICENSE
│       ├── README.md
│       ├── REVISION
│       └── kernel/
├── review-prompts/
│   └── .gitkeep
└── patchhound-review-knowledge/
    └── .gitkeep
```

## 随仓库分发的提示词

| 项目 | 来源 | 固定提交 | 本地用途 |
|---|---|---|---|
| Sashiko 提示词快照 | https://github.com/sashiko-dev/sashiko | `44d538b7e727e07504cd773e5f8ac0231a0f109a` | 提取 `third_party/prompts/kernel/`，供三个 expert 使用 |
| Sashiko 内嵌 review-prompts | https://github.com/masoncl/review-prompts | `4e9a9051bc4237b6543cda194d2143080127671d` | 提示词的上游版本，原始标记保留在 `sashiko/third_party/prompts/REVISION` |

保留 [Sashiko 原许可证](sashiko/LICENSE)、[提示词原许可证](sashiko/third_party/prompts/LICENSE)，以及提示词上游 README 和 REVISION。
本目录仅包含 Sashiko 的上述文件，不包含其 Rust 程序、网站、部署配置、其他子项目或 Git 元数据。

三个 expert 的默认 guidance 路径均为：

```text
third_party/sashiko/third_party/prompts/kernel
```

`kernel/` 保留现有的完整提示词目录及配套文件，共 118 个文件。相对上游，沿用此前发布整理的清理：

- 不分发 `kernel/sample.txt` 和 `kernel/examples/review-stat.txt` 样例报告。
- `kernel/README.md` 中对应的样例报告说明已改为省略提示。
- 本次缩减第三方目录没有修改任何 `kernel/` 文件。

当前 109 份 Markdown 按程序 `guidance_revision()` 的规则计算得到的快照标识为：

```text
b9c6ae3f8e1f8cdeeed1487b22d040fc6664199f0f8e337658624573a93f119d
```

该标识按相对路径排序，依次计算“路径 UTF-8 字节、NUL 分隔符、文件原始字节”的 SHA-256。
更新提示词时应显式更新来源版本和本快照说明，并检查三个 expert 的预览与测试。

## 可选参考仓库

`review-prompts/` 仅保留 `.gitkeep`，当前 agent 不读取该目录。原参考版本记录如下：

- 来源：https://github.com/masoncl/review-prompts
- 固定提交：`59469708305eca305cbd9eb94e5aa0ee3627529c`

如需查看该参考版本，可在项目外获取：

```bash
git clone --no-checkout https://github.com/masoncl/review-prompts.git /path/to/review-prompts-reference
git -C /path/to/review-prompts-reference checkout --detach 59469708305eca305cbd9eb94e5aa0ee3627529c
```

该参考版本与上面随仓库分发的 Sashiko 内嵌提示词版本不同。使用它替换默认 guidance 会改变模型输入。

`patchhound-review-knowledge/` 同样仅保留 `.gitkeep`，不分发知识库内容，也不是默认 expert 运行的必需项。

## 核对完整 Sashiko 上游

需要对照或重新提取上游文件时，可在项目外获取固定提交：

```bash
git clone --no-checkout https://github.com/sashiko-dev/sashiko.git /path/to/sashiko-upstream
git -C /path/to/sashiko-upstream checkout --detach 44d538b7e727e07504cd773e5f8ac0231a0f109a
```

本项目保留的是该提交的部分文件快照；获取完整上游不属于运行前置步骤。

## SDK 安装依赖

| SDK | 依赖声明 | 固定方式 |
|---|---|---|
| Codex Python SDK | [requirements-codex.txt](../requirements-codex.txt) | 固定到 `95637f7056835fea66bdd0044414af480fc0fd74` 的 `sdk/python` 子目录 |
| OMP | 两个 `src/experiments-omp-*` 目录的 `package.json`、`bun.lock` | 使用对应清单和锁文件安装 |
| OpenCode | 两个 `src/experiments-opencode-*` 目录的 `package.json`、`package-lock.json` | 使用对应清单和锁文件安装 |
| Python 运行依赖 | 根目录及各 sidecar 实现的 `pyproject.toml`、`uv.lock` | 使用 uv 安装 |

安装命令见项目根目录 [README](../README.md)。安装后的 SDK、虚拟环境和构建输出不分发到本目录。
