# danbooru 标签表 · 来源与说明

## 现有文件

| 文件 | 大小 | 行数 | 别名 | 数据日期 | 阈值 |
|---|---|---|---|---|---|
| `danbooru_2026-04-01_pt20-ia-dd.csv` | 5,079,171 B | 201,269 | 41,090 | 2026-04-01 | 发帖数 ≥ 20 |
| `danbooru_2024-10-14_legacy-top100k.csv` | 2,510,944 B | 100,000 | 19,648 | 2024-10 | top-100k 截断 |

**为什么要放两份**：新版删掉了一批旧标签（旧表 2.1%、共 2,139 个），其中不少是模型和
2024 年前后训练的底模都熟知的常用标签——`bangs`(145 万帖)、`silver_hair`(17 万)、
`black_legwear`、`white_legwear`、`uniform`、`looking_away`、`shiny_hair`、`striped`、
`plaid` 等。只用新表会把它们判成"生造标签"。节点默认把本目录下**所有** `danbooru*.csv`
按文件名倒序合并加载（新的优先），新旧两代标签都能用；`csv_path` 填了路径则只加载指定的
（多个用 `;` 分隔）。

`danbooru_2024-10-14_legacy-top100k.csv` 的来源是本机 A1111 的
`extensions\a1111-sd-webui-tagcomplete\tags\danbooru.csv`，也就是 tagcomplete 在
2024-11 改截断方式（top-100k → 发帖数 ≥ 30）之前的那一版。

## 来源

```
https://raw.githubusercontent.com/DraconicDragon/dbr-e621-lists-archive/main/tag-lists/danbooru/danbooru_2026-04-01_pt20-ia-dd.csv
```

- 归档仓库：https://github.com/DraconicDragon/dbr-e621-lists-archive
- 生成脚本：https://github.com/DraconicDragon/danbooru-e621-tag-list-processor
- 本机旧表（`novelai-webui-aki-v3\extensions\a1111-sd-webui-tagcomplete\tags\danbooru.csv`）与
  NeuralBooru 内置的那份，都是同一归档里的 `danbooru_2024-12-22_pt25-ia-dd.csv`（git blob sha 相同），
  而那份又更旧于本文件。

## 完整性

下载后按 git blob 校验：

```
sha1("blob " + len + "\0" + 内容) = 45992801b5c5dd5290b31b101a32438fd70041ad
```

与 GitHub 报告的 blob sha 一致（2026-09-21 校验）。

## 文件名含义

`danbooru_<日期>_pt<N>-ia-dd.csv`

- `pt20` = 发帖数阈值 20（越小收录越多）
- `ia`  = includes 'active' aliases，含生效别名
- `dd`  = includes danbooru deleted aliases，**别名表里也含已删除的别名**

## 列格式

```
标签名,类别,发帖数,"别名1,别名2,..."
```

类别：`0`=general，`1`=artist，`3`=copyright，`4`=character，`5`=meta
（名称用下划线；别名里含 `/lh` 这类 Danbooru 站内简写）

本文件类别分布：general 39,438 / artist 88,149 / copyright 12,728 / character 60,443 / meta 511

已验证：**废弃标签不会作为正式行出现**（`longhair` / `1girls` / `sole_female` 只在别名表里，
分别归一为 `long_hair` / `1girl`）。有 142 个标签同时是正式行、又是别的标签的别名，
归一逻辑里"先查别名、再查精确"可以把这 142 个也一并规范化。

## 与旧表（2024-10）的差别

| | 旧表 | 新表 |
|---|---|---|
| 标签行数 | 100,000（top-100k 截断） | 201,269（发帖数 ≥ 20） |
| 别名 | 19,648 | 41,090 |
| 发帖数规模 | 例：`1girl` 4,114,588 | 例：`1girl` 7,641,780（约 2 倍） |

注意：**发帖数整体翻倍**，所以任何"按发帖数过滤"的阈值都要按新表重新定，
沿用旧表上的经验值会误杀。

新增覆盖的典型例子：`pouting` 现在是 `pout` 的别名（旧表里查不到）。

## 更新方法

归档仓库每**月**由 GitHub Actions 产出 artifact，每**3 个月**（1/4/7/10）提交一次到仓库。
截至 2026-09-21，仓库里最新的一份是 2026-04-01——**7 月那次没有落地**，所以这就是目前能拿到的最新公开版本。

更新方式二选一：

1. 重新下载上面的 raw 链接（把日期换成新的文件名）。本机已有下载脚本：
   `agent_workspace_ignore\_wf_wire\dl_danbooru.py`（Python 标准库，含 blob sha 校验；注意本机
   `curl.exe` / PowerShell 的 TLS 在此沙箱下不可用，必须用 Python 下载）。
2. 直接从 Danbooru API 现抓一份（`/tags.json` 按发帖数排序 + `/tag_aliases.json`），
   可拿到当天最新数据，代价是要跑几分钟、需要控制请求频率。

## 用途

给"自然语言 → Danbooru 标签"工作流做**标签归一/校验**：把模型输出的写法
（`pouting`、`high angle shot`、`teal hair`）映射到词表里真实存在的标签，
或判定为生造标签丢弃。见 `agent_workspace_ignore\_wf_wire\评估-反向工作流首次生成.md`。
