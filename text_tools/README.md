# text_tools —— 提示词清洗 / 清洗版 CLIP 文本编码

给 ComfyUI 用的文本处理子包，包含两个节点，共用同一套清洗规则。

```
text_tools/
├── __init__.py          注册下面两个节点
├── prompt_clean.py      PromptClean：清洗 + 可选编码（左输入 / 右输出）
├── prompt_encode.py     PromptCleanEncode：清洗 + CLIP 文本编码（只读预览）
├── web/
│   └── prompt_clean.js  两个节点的前端脚本
└── README.md            本文件
```

## 节点一览

| 节点类型 | 显示名称 | 输入 | 输出 |
| --- | --- | --- | --- |
| `PromptClean` | 提示词清洗（去括号/权重） | `text`（左框输入）<br>`clip`（CLIP，可选） | `text`（清洗后的 STRING）<br>`clip`（CLIP 直通）<br>`conditioning`（接 clip 时才有值） |
| `PromptCleanEncode` | CLIP文本编码（清洗+显示） | `clip`（CLIP）<br>`text` | `conditioning` |

## PromptClean 的样子

```
┌───────────────────────────── 提示词清洗 ─────────────────────────────┐
│ ○ clip                                                               │
│ ┌─ 输入（原始提示词） ──────┐  ┌─ 输出（清洗后） ──────────────┐   │
│ │ (masterpiece:1.2), ...   │  │ masterpiece, ...              │   │
│ │                          │  │                               │   │
│ └──────────────────────────┘  └───────────────────────────────┘   │
│                                                text ○  clip ○  conditioning ○ │
└──────────────────────────────────────────────────────────────────────┘
```

- **左框可编辑**，就是原来的 `text` 输入；
- **右框只读**，执行后显示清洗结果（同时也通过 `ui.text` 回传前端）；
- 左框和原生 `text` 控件是**同一个数据源**：编辑左框会写回 `text`，保存工作流、排队时用的都是它；
- ComfyUI 原生控件只能整行竖排，没有“左右并排”的 API，所以这里是：
  用一个自定义 DOM 控件把两个 textarea 画在同一行，再把原生的 `text` 控件隐藏掉
  （它仍然存在、仍然序列化、仍然作为 `text` 输入送后端，只是不显示）。

### 三个输出

1. `text`：清洗后的字符串，可接原版 `CLIP文本编码` 的 text；
2. `clip`：CLIP 原样直通，方便串在 CLIP 和编码节点之间；
3. `conditioning`：**接上 `clip` 时**用 `clip.tokenize` + `clip.encode_from_tokens_scheduled`
   算出来的条件，可直接接 KSampler；不接 `clip` 时是 `None`。

> 注意：只要 `clip` 接了，就会算一次编码（即使你不用 conditioning 输出）。
> 这是有意的——节点输出必须只由输入决定，否则“有没有接 conditioning”会改变输出，
> 命中 ComfyUI 的缓存后可能拿到旧的 `None`。
> 所以如果后面还留着原版 `CLIP文本编码`，要么别接 `clip`（只用 text），
> 要么直接用 `conditioning` 输出、把后面的编码节点去掉，避免编码两次。

## 推荐接线

### 一个节点搞定

```
[Checkpoint 加载器] --CLIP--> [提示词清洗] --conditioning--> KSampler
                                  └-- text（清洗后，可选）
```

### 保留原版 CLIP 文本编码

```
[Checkpoint 加载器] --CLIP--> [提示词清洗] --CLIP--> [CLIP 文本编码] --> CONDITIONING
                                  └── text（清洗后）──┘
```

## 单独执行

`PromptClean` 的 `clip` 是可选输入，把它单独拖出来、在左框里贴提示词，
右键 → **Execute**（或直接跑队列），清洗结果就出现在右框里。

## 清洗规则

1. 删除 `(` `)` `{` `}` 四种括号字符（`((a))`、`{a}` → `a`）；
2. 删除冒号权重 `:1.2`、`: 0.9`、`:2`、`:.5`；
3. 其余字符**一个不动**：逗号、空格、换行都保留。

例：

```
输入：..., vaginal dripping pee:1.2, (Pissing light is transparent:0.9), ...
输出：..., vaginal dripping pee, Pissing light is transparent, ...
```

为避免误伤，权重只在“像权重”的位置删除：

- 冒号前一位是数字时不删：`16:9`、`8:00`、`1080:1920` 原样保留；
- 数字后面不是 `,` `)` `]` `}` / 行尾时不删：`color:red` 原样保留。

不处理 `[ ]`（SD 的降权括号）。

## 兼容 / 注意事项

- 旧工作流里 `PromptClean` 只存了 2 个输出，加载时前端会**自动补上第 3 个
  `conditioning`**（`prompt_clean.js` 里的 `ensureOutputs`）。
- 新节点默认 **760×320**；旧工作流里节点保持你保存时的尺寸。两个框是自适应的，
  往宽拖一点会更舒服（比如 900×300）。
- 隐藏的原生 `text` 控件不会再显示，但它的值仍然会被保存、被送去排队；
  如果哪天想恢复成普通节点，删掉 `web/prompt_clean.js` 即可。
- 改 Python / JS 后需要**重启 ComfyUI**；浏览器 `Ctrl+F5` 强刷一次。
- 前端脚本失败时节点仍能执行（只是没有左右布局），可在控制台看 `[PromptClean]` 报错。
