# horizontalSight-custom-nodes

**作者：horizontalSight。**

项目由AI辅助构建，如果出现任何问题，都是正常情况。

以下内容由AI构建。



horizontalSight-custom-nodes自定义节点。

 伞包（facade）：把原先 5 个独立的 `horizontalSight-*` 包合并成一个包 + 内部功能分类，参照 `comfyui-kjnodes` 的组织方式。

节点**类型名与合并前完全一致**，既有工作流不需要任何改动。

## 安装

```
ComfyUI\custom_nodes\horizontalSight-custom-nodes\
```

重启 ComfyUI。启动日志会打印一行确认：

```
[horizontalSight] 伞包已加载: nodes.image_folder=2, nodes.sequence_save=1, nodes.memory=1, nodes.booru_tags=1, llama_cpp=12 | 节点总数 17
```

同时 `Enabled pinned memory ...` 这一行应当**消失**（见下方 `prestartup_script.py`）。

`llama_cpp` 需要 `llama-cpp-python`；其余节点只用 ComfyUI 自带的 Pillow / numpy / torch，
不需要额外依赖。

## 节点总览（17）

### 本地节点 `nodes/`（5）

| 节点类型                   | 显示名         | 分类             | 作用                                                                                               |
| ---------------------- | ----------- | -------------- | ------------------------------------------------------------------------------------------------ |
| `LoadImagesFromFolder` | 从文件夹读图      | `image/io`     | 把文件夹里所有图片读成一个 batch，并把文件夹路径透传给保存节点                                                               |
| `SaveImagesWithPrompt` | 保存图片(嵌入提示词) | `image/io`     | 回读原图另存，把运行中的工作流嵌进 PNG，并把每张图对应的提示词写进工作流节点                                                         |
| `SequenceImageSave`    | 顺序保存图像      | `image/io`     | 按 `序号_前缀.ext` 保存，同一文件夹跨前缀连续编号；支持 `[time(%Y-%m-%d)]` 占位符                                          |
| `ClearMemory`          | 清理显存        | `utils`        | 卸载全部模型并清空显存缓存，**含 SeedVR2 常驻的 DiT/VAE 缓存**（其他插件清不掉这部分）；输入原样透传                                    |
| `BooruTagNormalize`    | Booru 标签归一  | `prompt/booru` | 把 LLM 输出的标签串对齐到本地 Danbooru 词表：别名 → 精确 → 词形 → 可编辑短语表；输出 `tags` / `dropped` / `unknown` / `report` |

### `llama_cpp/`（12）

自己的 fork，上游是 [lihaoyun6/ComfyUI-llama-cpp_vllm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vllm)。

| 节点类型                     | 显示名                    |
| ------------------------ | ---------------------- |
| `llama_cpp_model_loader` | Llama-cpp Model Loader |
| `llama_cpp_instruct_adv` | Llama-cpp Instruct     |
| `llama_cpp_parameters`   | Llama-cpp Parameters   |
| `llama_cpp_unload_model` | Llama-cpp Unload Model |
| `llama_cpp_clean_states` | Llama-cpp Clean States |
| `parse_json_node`        | Parse JSON             |
| `json_to_bbox`           | JSON to BBoxes         |
| `bbox_to_segs`           | BBoxes to SEGS         |
| `bbox_to_mask`           | BBoxes to MASK         |
| `bboxes_to_bbox`         | BBoxes to BBox         |
| `remove_code_block`      | Unpack Code Block      |
| `PromptEnhancerPreset`   | Prompt Enhancer Preset |

## 目录结构

```
horizontalSight-custom-nodes/
├── __init__.py            门面：汇总各子模块的 NODE_CLASS_MAPPINGS（含冲突保护）
├── prestartup_script.py   启动期脚本：关掉 ComfyUI 0.30 的 pinned-memory 权重驻留
├── nodes/                 本地节点（每个文件独立可读）
│   ├── image_folder.py    LoadImagesFromFolder / SaveImagesWithPrompt
│   ├── sequence_save.py   SequenceImageSave
│   ├── memory.py          ClearMemory
│   └── booru_tags.py      BooruTagNormalize（词表在 ../danbooru/）
├── danbooru/              Danbooru 标签表 CSV（BooruTagNormalize 的数据源）
├── llama_cpp/             12 个 llama.cpp 节点（上游 fork）
└── example_workflows/     示例工作流 JSON，可直接拖进 ComfyUI
```

## 三个刻意的设计决定

1. **`prestartup_script.py` 必须待在包根**
   `main.py` 只扫描 `custom_nodes/<包名>/prestartup_script.py` 这一层，放进子文件夹不会被执行。
   它在 `model_management` 初始化前把 `args.disable_pinned_memory` 置为 `True`，等效于启动时加
   `--disable-pinned-memory`，避免大模型权重以 pinned 副本同时在显存和内存里各驻留一份。

2. **子模块之间没有重名，但门面仍有冲突保护**
   若出现同名节点，日志会报 `[horizontalSight] 节点类型名冲突: xxx`，保留先加载的那个，
   而不是静默覆盖。

3. **任何一个子模块加载失败，其余节点照常可用**
   门面逐个 `importlib.import_module` 并 try/except 隔离，失败只在日志里报一行。

## 新增节点

在 `nodes/` 下加一个 `.py`，暴露 `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`，
然后在门面 `__init__.py` 的加载列表 `("image_folder", "sequence_save", "memory", "booru_tags")` 里加一项。
改完重启 ComfyUI。

## example_workflows/

`example_workflows/` 是随仓库分发的示例工作流，按用途/模型分类：`A1111（SD webui）/`、
`Krea2/`、`llama/`、`MiniMaxH3/`、`qwen2.1/`、`Zimage/`，共 11 个 JSON，可直接拖进 ComfyUI
画布使用。它们不是 ComfyUI 运行时的一部分，ComfyUI 不会加载它们。
