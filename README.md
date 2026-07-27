# hf-sync

在 HuggingFace 与 ModelScope 之间**直接**同步模型 / 数据集的命令行工具 —— 无需下载整个模型/数据集到本地。

## 为什么需要 hf-sync

对于在国内访问 Hugging Face 有网络困难的用户来说，从 ModelScope 下载模型 / 数据集会快得多。
因此，将 HF 上的模型 / 数据集同步到 ModelScope 是一个常见需求。

Hugging Face 和 ModelScope 各自提供了命令行工具（`huggingface-cli`、`modelscope`），
可以分别下载和上传模型 / 数据集。但如果要用它们做跨平台同步，传统做法是先把完整仓库下载到本地，
再上传到目标平台：

- **必须在本地磁盘上存放完整的仓库副本**——一个 100GB 的模型就要占用 100GB 本地空间
- 下载和上传是两个独立步骤，需要手动操作，无法一键完成

`hf-sync` 逐文件流式中转，**不需要先将整个仓库完整的模型 / 数据集下载到本地再上传**，
同一时刻仅在本地暂存当前正在传输的文件。同时提供增量同步、断点续传、并发传输等能力，
把跨平台同步简化为一条命令。

## 功能概览

- **跨平台同步**：HF ↔ ModelScope 双向同步模型 / 数据集
- **同平台同步**：HF → HF、MS → MS，用于仓库备份 / 改名 / 迁移
- **本地目录支持**：任一端可替换为本地目录，作为纯下载 / 上传工具使用
- **流式中转**：逐文件流式传输，不需要在本地保留整个仓库的完整副本，同一时刻仅在本地暂存当前正在传输的文件
- **增量同步**：自动对比文件列表和内容哈希，仅传输新增 / 有变更的文件
- **断点续传**：大文件同步中断后可从上次位置继续下载，无需从头重传
- **并发传输**：默认 5 个文件并行传输，可通过 `--concurrency` 调整
- **安全确认**：写入前显示同步计划并要求手动确认，`-y` 可跳过
- **镜像模式**：`--delete` 删除目标端多余文件，使两端完全一致（类似 `rsync --delete`）
- **预览模式**：`--dry-run` 仅打印同步计划而不实际传输
- **路径排除**：`--exclude` 按 glob / 目录前缀排除特定文件

## 快速开始

### 安装

```bash
pip install -e .
```

### 基本用法

```bash
# 跨平台同步模型（默认仓库类型）
hf-sync sync hf:<namespace>/<repo> ms:<namespace>/<repo>

# 同步数据集
hf-sync sync --repo-type dataset hf:<namespace>/<repo> ms:<namespace>/<repo>

# 反向同步同理
hf-sync sync ms:<namespace>/<repo> hf:<namespace>/<repo>

# 同平台同步（备份 / 迁移）
hf-sync sync hf:org/model hf:org/model-backup

# 下载到本地目录（任一端可替换为本地路径）
hf-sync sync hf:org/model /path/to/local/dir

# 从本地目录上传到 hf/ms（不存在则自动创建仓库）
hf-sync sync /path/to/local/dir ms:org/model
```

### 认证

```bash
hf-sync sync hf:org/model ms:org/model \
  --hf-token $HF_TOKEN \
  --ms-token $MS_TOKEN
```

- `--hf-token`：访问 HF 上的 gated / 私有仓库时需要；当 HF 作为**目标**（写入端）时始终需要。
- `--ms-token`：访问 ModelScope 私有仓库时需要；当 ModelScope 作为**目标**时始终需要。

### 作为下载 / 上传工具

`source` / `target` 只要不是 `hf:` / `ms:` 开头，就会被当作本地目录路径：

```bash
# 下载：hf/ms 上的仓库 -> 本地目录
hf-sync sync --repo-type dataset hf:TianxingChen/RoboTwin2.0 /path/to/local/dir

# 上传：本地目录 -> hf/ms（不存在则自动创建仓库）
hf-sync sync --repo-type dataset /path/to/local/dir hf:TianxingChen/RoboTwin2.0
```

本地目录一侧的注意事项：

- 目录不存在时，作为下载目标会自动创建；作为上传源则要求目录已存在且非空。
- 增量对比、`--dry-run`、`--force`、`--delete`、`--exclude`、`--concurrency` 等选项同样生效；
  由于本地文件不计算内容哈希，增量对比按「路径 + 文件大小」判断。
- `--revision` / `--dst-revision` 对本地目录一侧无意义，会被忽略。
- source 和 target 不能同时是本地目录。

## 核心机制

### 流式中转

Hugging Face 和 ModelScope 均不提供服务器到服务器的 "从 URL 导入" API，因此数据必须经过运行
`hf-sync` 的机器中转。`hf-sync` **逐文件流式传输**，不需要在本地保留整个仓库的完整副本——
同一时刻内存中仅有当前正在传输的那个文件的数据，传输完成后立即释放。
在不受上游 SDK 限制的理想路径下（文件 ≤ 256MiB 且目标端为 HF / ModelScope），
单个文件的数据以小块从源端读取后直接转发到目标端的上传请求中，全程在内存中流过，
不会被完整缓冲，也不会被写入本地磁盘。

两个平台的上传流程都需要读取文件内容两次——一次计算内容哈希，一次实际传输。在理想路径下（文件 ≤ 256MiB
且目标端为 HF / ModelScope），由于 HTTP 响应流不支持随机寻址（seek），`hf-sync` 的流包装器
（`hf_sync/remote_stream.py`）在收到 "回到开头" 请求时会透明地向源端重新发起 GET 请求，而非将数据缓冲到
磁盘。实际影响：当目标端不知道文件哈希时，源端带宽使用约为文件大小的 2 倍；但本地内存使用量始终保持
恒定，与文件大小无关。对于 > 256MiB 的大文件，源端数据只下载一次到本地临时文件，后续的哈希计算和
上传都从本地文件读取，不再回源。

单文件 > 256MiB，或目标端为本地目录，当前传输的文件会临时落地到本地磁盘，
详见 [「临时文件与断点续传」](#临时文件与断点续传)。

### 增量同步

每次运行会先列出两端的文件列表。如果目标端已存在同名且内容哈希匹配的文件（或当某一端无法获取哈希时，
按文件大小匹配），则跳过该文件。仅传输新增 / 有变更的文件。使用 `--force` 可忽略增量差异强制重新上传
所有文件，使用 `--dry-run` 可仅预览同步计划而不实际传输。

`.gitattributes` 默认排除，因为该文件由各平台独立维护，同步会导致每次都重复上传。

### 临时文件与断点续传

`hf-sync` 的设计目标是**不需要在本地保留整个仓库的完整副本**，逐文件流式传输，同一时刻仅在本地暂存当前正在传输的文件。大多数文件不会落地本地磁盘，但在以下情况下文件会先写入本地临时文件（`.part`），完成后再替换为最终文件：

- **单文件 > 256MiB**：上游 SDK（`huggingface_hub`、`modelscope_hub`）在接收文件流对象时会一次性
  `.read()` 整个文件到内存来计算哈希，大文件容易导致 OOM。为规避这个上游限制，超过 256MiB 的文件
  会先落地到本地临时文件，再交给 SDK 走分块哈希 + 磁盘流式上传的安全路径。
- **目标端为本地目录**：文件先写入目标路径旁的 `.part` 临时文件，下载完成后才原子替换
  （`os.replace`）为最终文件名，确保目标目录中不会出现半成品文件。

这些临时文件采用稳定路径（而非随机临时文件），因此如果同步中途中断（`Ctrl-C`、断网、进程崩溃等），
已下载的部分会保留在磁盘上，下次同步相同文件时可以从中断处续传（通过 HTTP Range 请求），而不是
重新下载。同步成功后临时文件会被自动删除；如果上传失败，会保留已下载的部分以便下次续传。

临时文件位置：

- **HF / ModelScope 作为目标端**：位于系统临时目录下的 `hf-sync-partial/` 子目录中
  - macOS：一般是 `$TMPDIR/hf-sync-partial/`（形如 `/var/folders/xx/xxxxxxxx/T/hf-sync-partial/`）
  - Linux：一般是 `/tmp/hf-sync-partial/`
- **本地目录作为目标端**：`.part` 文件就位于目标目录中对应文件的旁边（如
  `dir/model.safetensors.part`），下载完成后自动替换为 `dir/model.safetensors`

如果临时目录所在磁盘空间不足以容纳单个最大文件，同步会失败；可以通过设置环境变量 `TMPDIR`
把它指向一个空间更充足的目录后再运行 `hf-sync`。同步结束后如果有残留的未完成断点续传文件，
命令行会输出警告提示其路径和占用空间，可手动删除以释放磁盘。

### 传输进度

每个文件同步时会显示一个 `tqdm` 进度条（文件序号 / 总数、文件名、已传输字节数、速度）。由于底层 SDK
需要把文件读两遍（一遍算哈希，一遍传输），进度条会出现两次：第一次标记为 `↓`（下载 / 哈希计算），
第二次自动切换为 `↑`（上传）；遇到 `seek(0)` 时会重置为 0 而非显示"倒退"。并发传输时各文件进度条
分行显示互不干扰。

### 覆盖 / 创建确认

真正执行写入前（`--dry-run` 除外），`hf-sync` 会根据目标仓库是否已存在打印提示：

- 目标仓库已存在 → 提示将新增 / 覆盖 N 个文件（默认不删除多余文件，除非加了 `--delete`）
- 目标仓库不存在 → 提示将创建该仓库并写入 N 个文件

随后要求手动输入 `y` / `N` 确认（大小写不敏感，直接回车默认为 `N`）。使用 `-y` / `--yes`
可跳过确认，适合脚本化场景。若没有文件需要同步（且未启用 `--delete`），则直接退出。

### 镜像模式（`--delete`）

加上 `--delete` 后，会额外把「存在于目标、但源端已没有」的文件删除，让目标仓库与源仓库完全一致
（类似 `rsync --delete`）。

- 确认提示会同时显示将新增 / 覆盖多少个文件、将删除多少个文件。
- **HF 作为目标**：删除通过一次 `create_commit`（`CommitOperationDelete`）完成，token 权限足够即可。
- **ModelScope 作为目标**：`delete_files` 目前要求 cookie 会话登录，仅凭 API token（`ms-...`）
  可能返回 401。若遇到此问题，需要先用 `modelscope login` 完成一次浏览器登录，
  或手动在 ModelScope 网页控制台删除多余文件。

## ⚠️ 带宽与网络依赖

`hf-sync` 是在**运行它的这台机器**上做中转的客户端工具：源端文件先下载到本机，再上传到目标端，
两个云平台之间没有直连通道。这意味着：

- **会占用本地网络流量**，且流量规模跟仓库总大小同一个量级。理想路径下（文件 ≤ 256MiB 且目标端为
  HF / ModelScope），每个文件要从源端读两遍（一遍算 hash，一遍传输），本地总流量大约是文件大小的
  **~3 倍**（源端下行 ~2x + 目标端上行 ~1x）；大文件（> 256MiB）先落地本地再上传，源端只读一遍，
  流量约为 **~2 倍**（源端下行 ~1x + 目标端上行 ~1x）。
- **同步速度受限于本机的上下行带宽**，跟两个云平台之间的带宽无关。上传带宽 20Mbps（~2.5MB/s）时，
  光上传 100GB 就需要约 11 小时。`hf-sync` 默认并发 5 个文件以更好地利用带宽，但单文件较大时
  瓶颈仍在单个传输流水线，并发主要在多文件场景下生效。
- 单个大文件中途 `Ctrl-C` 或断网会导致该文件传输失败，需要重新运行（已成功的文件会自动跳过）。

**建议**：如果本地带宽有限，把 `hf-sync` 放到一台带宽更好、离目标机房更近的云主机上运行
（阿里云 / 腾讯云 / AWS 等），可以显著提速并且不占用本机带宽。

## 命令行参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `source` | 源引用：`hf:<namespace>/<repo>`、`ms:<namespace>/<repo>`，或本地目录路径 | — |
| `target` | 目标引用，格式同上 | — |
| `--repo-type` | 仓库类型：`model` 或 `dataset` | `model` |
| `--hf-token` | Hugging Face 访问令牌 | 无 |
| `--ms-token` | ModelScope 访问令牌 | 无 |
| `--revision` | 源端分支 / 版本 | HF 为 `main`，ModelScope 为 `master` |
| `--dst-revision` | 目标端分支 / 版本 | 同上，按目标平台决定 |
| `--commit-message` | 目标仓库的提交信息 | `Sync via hf-sync` |
| `--force` | 忽略增量差异，强制重新上传所有文件 | 否 |
| `--delete` | 删除目标仓库中源端已不存在的多余文件，使两端完全一致 | 否 |
| `-c` / `--concurrency` | 同时并发传输的文件数 | `5` |
| `--dry-run` | 仅打印同步计划，不实际传输 | 否 |
| `--private` | 创建目标仓库时设为私有（默认行为，此参数用于显式声明） | 是 |
| `--public` | 创建目标仓库时设为公开，覆盖默认的私有行为 | 否 |
| `-y` / `--yes` | 跳过覆盖 / 创建确认提示，直接执行 | 否 |
| `--exclude` | 排除指定路径 / glob / 目录不参与同步，可重复使用（如 `--exclude data/`）。`.gitattributes` 默认排除 | 无 |
| `-v` / `--verbose` | 输出详细日志 | 否 |

## 项目结构

```
hf_sync/
├── uri.py                  # 解析 hf: / ms: 仓库引用或本地目录路径
├── remote_stream.py        # 可重新打开的流式文件对象（逐文件流式中转）
├── progress.py             # tqdm 进度条包装器
├── sync.py                 # 差异比较与传输编排
├── cli.py                  # 命令行入口
└── providers/
    ├── __init__.py          # Provider 工厂
    ├── base.py              # Provider 抽象基类
    ├── hf_provider.py       # Hugging Face Provider
    ├── ms_provider.py       # ModelScope Provider
    ├── local_provider.py    # 本地文件系统 Provider
    └── resumable.py         # 断点续传共享逻辑（spool_to_file 等）
```

## 已知限制

- ModelScope 用于列出 / 下载文件的 REST 接口（`/api/v1/{models|datasets}/<repo>/repo(...)`）
  与官方客户端使用的接口相同，但不属于稳定的公开 OpenAPI 契约——如果 ModelScope 更改了这些路径，
  只需更新 `hf_sync/providers/ms_provider.py`。
- 超大单文件（多 GB）依赖底层 SDK（`huggingface_hub`、`modelscope`）自身的分片 / LFS 上传逻辑；
  上传过程中出现网络中断需要重新运行同步（已提交的文件会通过增量差异自动跳过）。
- ModelScope 端删除文件可能需要 cookie 会话登录，详见[镜像模式](#镜像模式---delete)一节。
