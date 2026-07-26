# hf-sync

在 Hugging Face Hub 与 ModelScope 之间**直接**同步模型 / 数据集（也支持同平台同步，如 HF -> HF），
类似于 `skopeo` 在不同镜像仓库之间复制容器镜像 —— 整个过程不会在本地磁盘上落地完整文件。

```bash
# 同步模型（默认仓库类型）
hf-sync sync hf:<namespace>/<repo> ms:<namespace>/<repo>

# 同步数据集
hf-sync sync --repo-type dataset hf:<namespace>/<repo> ms:<namespace>/<repo>

# 反向同步同理
hf-sync sync ms:<namespace>/<repo> hf:<namespace>/<repo>
```

## ⚠️ 带宽与网络依赖（重要）

`hf-sync` 是在**运行它的这台机器**上做中转的客户端工具：源端的文件会先下载到这台机器，再上传到目标端，
两个云平台之间没有直连通道。这意味着：

- **会占用本地网络流量**，且流量规模跟仓库总大小同一个量级 —— 当前实现里每个文件通常要从源端读两遍
  （一遍算 hash，一遍实际传输），所以本地总流量大约是文件大小的 **~3 倍**（源端下行 ~2x + 目标端上行 ~1x）。
  同步一个 100GB 的模型/数据集，本地大概会产生 ~300GB 的上下行流量。
- **同步速度直接受限于本机的上下行带宽**，跟两个云平台之间的带宽无关。比如上传带宽只有 20Mbps
  （~2.5MB/s）时，光上传 100GB 就需要 11 小时左右；家庭网络不稳定/限速也会导致同步变慢或中断。
  `hf-sync` 默认同时传输 5 个文件（可通过 `--concurrency` 调整），以更好地利用上下行带宽；
  但单文件较大时瓶颈仍在单个传输流水线，并发主要在多文件场景下生效。
- 单个大文件（如几 GB 的模型权重）在没有网络问题的情况下也会占用较长时间的本机带宽，中途 `Ctrl-C`
  或断网会导致该文件传输失败，需要重新运行（已成功同步的文件会在增量对比中自动跳过，不会重复传输）。

**建议**：如果本地带宽有限，把 `hf-sync` 放到一台带宽更好、离目标机房更近的云主机上运行（阿里云/腾讯云/
AWS 等），而不是家庭网络或笔记本本地网络，可以显著提速并且不占用你本机的带宽。

## 安装

```bash
pip install -e .
```

## 认证

```bash
hf-sync sync hf:org/model ms:org/model \
  --hf-token $HF_TOKEN \
  --ms-token $MS_TOKEN
```

- `--hf-token`：访问 HF 上的 gated / 私有仓库时需要；当 HF 作为**目标**（写入端）时始终需要。
- `--ms-token`：访问 ModelScope 私有仓库时需要；当 ModelScope 作为**目标**时始终需要。

## 传输进度

每个文件同步时会显示一个 `tqdm` 进度条（当前文件序号 / 总数、文件名、已传输字节数、速度）。由于底层 SDK
通常需要把文件读两遍（见下文"不落地本地"一节），进度条可能会出现两次：一次是读取/计算哈希，一次是实际
上传；遇到 `seek(0)`（重新从源端读取）时会自动重置为 0，而不是显示成"倒退"。

## 增量同步

每次运行会先列出两端的文件列表。如果目标端已存在同名且内容哈希匹配的文件（或当某一端无法获取哈希时，
按文件大小匹配），则跳过该文件。仅传输新增 / 有变更的文件。使用 `--force` 可忽略增量差异强制重新上传
所有文件，使用 `--dry-run` 可仅预览同步计划而不实际传输。

## 覆盖/创建确认

真正执行写入前（`--dry-run` 除外），`hf-sync` 会根据目标仓库是否已存在打印提示：

- 目标仓库已存在 → 提示将新增/覆盖 N 个同名文件（默认不会删除目标仓库中多余的文件，除非加了 `--delete`）
- 目标仓库不存在 → 提示将创建该仓库并写入 N 个文件

随后要求手动输入 `y`/`N` 确认（大小写不敏感，直接回车默认为 `N`，即取消）。使用 `-y` / `--yes`
可跳过该确认，适合脚本化/非交互场景。若没有文件需要同步（且未启用 `--delete`），则直接退出，不会弹出确认。

## 镜像模式（`--delete`）

默认情况下 `hf-sync` 只做增量新增/覆盖，不会删除目标仓库中源端已经没有的文件。加上 `--delete` 后，
会额外把「存在于目标、但源端已没有」的文件也删除掉，让目标仓库与源仓库完全一致（类似 `rsync --delete`）。

- 确认提示会同时显示将新增/覆盖多少个文件、将删除多少个文件。
- **HF 作为目标**：删除通过一次 `create_commit`（`CommitOperationDelete`）完成，token 权限足够即可。
- **ModelScope 作为目标**：ModelScope 的 `delete_files` 目前官方要求 cookie 会话登录，仅凭 API token
  （`ms-...`）可能会返回 401。若遇到此问题，需要先用 `modelscope login` 完成一次浏览器登录，
  或者手动在 ModelScope 网页控制台删除多余文件。

## "不落地本地" 的实现原理

Hugging Face 和 ModelScope 均不提供服务器到服务器的 "从 URL 导入" API，因此数据必须经过运行
`hf-sync` 的机器中转 —— 这一点无法绕过（`skopeo` 同样如此，它也是将 blob 流经自身而非真正的零拷贝传输）。
`hf-sync` 能保证的是：**完整文件永远不会被写入本地磁盘，也不会被完整缓冲在内存中**。数据从源端以小块读取，
直接转发到目标端的上传请求中。

一个细节：两个平台的上传流程都需要读取文件内容两次 —— 一次计算内容哈希，一次实际传输。由于 HTTP 响应流
不支持随机寻址（seek），`hf-sync` 的流包装器（`hf_sync/remote_stream.py`）在收到 "回到开头" 请求时，
会透明地向源端重新发起 GET 请求，而非将数据缓冲到磁盘。实际影响：当目标端不知道文件哈希时，源端带宽使用
约为文件大小的 2 倍；但本地磁盘 / 内存使用量始终保持恒定，与文件大小无关。

**已知例外（ModelScope 作为目标端 + 大文件）**：`modelscope_hub` SDK 的 `upload_file()` 在接收非
str/Path/bytes 的文件对象时，会不做分块地一次性 `.read()` 整个文件到内存来计算哈希（并保留这份内存
拷贝用于后续上传），大文件（几 GB 级别）很容易触发系统内存不足保护机制直接杀掉进程（现象是命令行只打印
`Terminated`，没有任何 Python 异常）。为规避这个上游 SDK 限制，`hf_sync/providers/ms_provider.py` 中
超过 256MiB 的文件在上传到 ModelScope 时会先落地到一个本地临时文件（用完立即删除），再把文件路径交给
SDK，从而走它分块哈希 + 磁盘流式上传的安全路径。也就是说：**目标是 ModelScope 且单文件超过 256MiB
时，会临时占用等同于该文件大小的本地磁盘空间**（处理完立刻释放），这是当前唯一违反"零本地磁盘"设计的
情况。HF 作为目标端不受影响（`huggingface_hub` 的哈希实现是分块的）。

临时文件会写在系统默认临时目录下（Python `tempfile.NamedTemporaryFile()` 的默认行为，不额外指定
`dir` 参数），文件名形如 `hf-sync-XXXXXXXX.part`：

- macOS：一般是 `$TMPDIR`（形如 `/var/folders/xx/xxxxxxxx/T/`）
- Linux：一般是 `/tmp`

如果这个目录所在磁盘空间不足以容纳单个最大文件，同步会失败；可以通过设置环境变量 `TMPDIR`
（macOS/Linux 通用）把它指向一个空间更充足的目录后再运行 `hf-sync`。该临时文件在上传成功或失败后都会
被立即删除（`finally` 块保证），不会常驻。

## 命令行参数

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `source` | 源仓库引用，格式为 `hf:<namespace>/<repo>` 或 `ms:<namespace>/<repo>` | — |
| `target` | 目标仓库引用，格式同上 | — |
| `--repo-type` | 仓库类型：`model` 或 `dataset` | `model` |
| `--hf-token` | Hugging Face 访问令牌 | 无 |
| `--ms-token` | ModelScope 访问令牌 | 无 |
| `--revision` | 源端分支 / 版本 | HF 为 `main`，ModelScope 为 `master` |
| `--dst-revision` | 目标端分支 / 版本 | 同上，按目标平台决定 |
| `--commit-message` | 目标仓库的提交信息 | `Sync via hf-sync` |
| `--force` | 忽略增量差异，强制重新上传所有文件 | 否 |
| `--delete` | 删除目标仓库中源端已不存在的多余文件，使两端完全一致 | 否 |
| `--concurrency` | 同时并发传输的文件数（多个下载/上传流水线并行），用于跑满上下行带宽 | `5` |
| `--dry-run` | 仅打印同步计划，不实际传输 | 否 |
| `--private` | 如果需要创建目标仓库，则创建为私有仓库 | 否 |
| `-y` / `--yes` | 跳过覆盖/创建确认提示，直接执行 | 否 |
| `-v` / `--verbose` | 输出详细日志 | 否 |

## 项目结构

- `hf_sync/uri.py` — 解析 `hf:` / `ms:` 仓库引用
- `hf_sync/remote_stream.py` — 可重新打开的流式文件对象
- `hf_sync/providers/` — `HFProvider` / `MSProvider`：列出文件、打开读取流、创建仓库、上传
  - `hf_sync/providers/base.py` — Provider 抽象基类
  - `hf_sync/providers/hf_provider.py` — Hugging Face Provider 实现
  - `hf_sync/providers/ms_provider.py` — ModelScope Provider 实现
- `hf_sync/sync.py` — 差异比较与传输编排
- `hf_sync/cli.py` — 命令行入口

## 已知限制 / 需要针对你的账号验证的事项

- ModelScope 用于列出 / 下载文件的普通 REST 接口
  （`/api/v1/{models|datasets}/<repo>/repo(...)`）与 ModelScope 官方客户端使用的接口相同，
  但不属于稳定的公开 OpenAPI 契约 —— 如果 ModelScope 更改了这些路径，只需更新
  `hf_sync/providers/ms_provider.py` 即可。
- 超大单文件（多 GB）依赖底层 SDK（`huggingface_hub`、`modelscope`）自身的分片 / LFS 上传逻辑，
  从提供的文件流中读取数据；上传过程中出现网络中断需要重新运行同步（已提交的文件会通过增量差异在重试时自动跳过）。
- 源端的仓库 / 文件删除**不会**被同步（没有删除步骤）；`hf-sync` 仅添加 / 更新文件。
