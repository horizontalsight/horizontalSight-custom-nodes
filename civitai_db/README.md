# civitai_db

Civitai 数据库管理面板 + 5 个 ComfyUI 节点。功能来自 `civitai_batch_metadata`
的「更新数据库」页和 `civitai_ckpt_inject` 的 PNG 注入，但在这里重新实现，
不依赖那两个项目。

## 节点

| 类型 | 显示名 | 作用 |
|---|---|---|
| `CivitaiDBPanel` | Civitai 数据库面板 | 节点按钮打开 Web 面板 |
| `CivitaiModelLookup` | 查询 Civitai AIR | 按文件名 / 相对路径 / 绝对路径查本地库 |
| `CivitaiDBUpdate` | Civitai 数据库维护 | 扫描、校验路径、增量/全量哈希、联网查询 |
| `CivitaiAirsFromWorkflow` | 从工作流提取 AIR | 读当前 `PROMPT` + `workflow`，输出 AIR 列表 |
| `CivitaiInjectImages` | Civitai PNG 注入 | 批量给 PNG 注入 AIR，并修复 Civitai 提示词 |

`CivitaiDBPanel` 节点上提供「打开 Civitai 数据库面板」「打开 PNG 注入」两个按钮，
在浏览器新标签页打开面板。`CivitaiInjectImages` 节点上也有「打开面板 · PNG 注入」
按钮。面板本身是独立页面，不在画布内嵌：

```
http://127.0.0.1:8188/db/
```

（端口以你的 ComfyUI 启动端口为准。）

## 面板

- **模型库**：筛选、排序、右键菜单、编辑抽屉、批量操作、体检、维护按钮。
  顶部「模型目录」按本地路径前缀筛选，默认自动填 `<ComfyUI>/models`；
  「全部」可清空筛选。
- **PNG 注入**：输入/输出目录、后缀、覆盖、kinds、提示词修复。
  「预览」只报告；「执行注入」才写文件。已有输出默认跳过，勾选「覆盖已有输出」才重做。
- **设置**：DB 路径、ComfyUI 根目录（默认从插件安装位置推断，可手改）、API 地址、
  超时/重试/间隔、扫描类别/后缀、哈希缓存、备份。

维护顺序建议：`扫描登记` → `增量哈希` → `联网查询（新增）`。
之前查询过但 404 的记录会标成「未找到」，普通新增查询不会重复打；
需要时用「重查未找到」。

## 数据

本机数据都在 `civitai_db/_local_config_ignore/`（已被 git 忽略）：

```
model_db.json       本地模型库（主键 AutoV2）
config.json         设置
hash_cache.json     (路径, 大小, mtime) -> SHA256 缓存
backups/            每次写库前的备份
app.log             日志
```

首次使用可以把旧的 `model_db.json` 放到这个目录；设置里的
「首次导入旧缓存」可以指向旧的 `sha256.json`，避免重新哈希大模型。
**不要把 `_local_config_ignore/` 提交到公共仓库**，里面有本机绝对路径。

## 行为说明

- 数据库损坏时拒绝覆盖，原文件不动；写库前会备份。
- 404 是终态，不自动重试；网络/429/5xx 归为「查询失败」，可单独重试。
- 连续 429 会暂停整批，剩余记录保持原状态。
- PNG 注入只替换 `prompt` / `workflow` 两个文本块，其余块逐字节保留；
  写盘前有块级自检，失败不落盘。
- `CivitaiInjectImages` 默认 `dry_run=True`；确认后再取消勾选执行。
- 旧 webui 链接目录属于正常路径，不会被当成错误清理。

## 依赖

纯 Python 标准库 + ComfyUI 自带依赖；不需要 Pillow，也不依赖
`civitai_batch_metadata` / `civitai_ckpt_inject`。
