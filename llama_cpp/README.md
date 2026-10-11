# ComfyUI-llama-cpp(个人 fork)

> **[[English](./README_en.md)]**　·　个人 fork 镜像版

> **本仓库是第三方 fork,不是原作者的作品。**

本仓库是对 [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm) 的一个派生副本，基于其 commit `c3a6314`(2026-06-13)。代码主体、图片、文档均来自原作者,版权归原作者及其贡献者所有；本仓库仅做个人使用与备份。

如原作者或相关权利方对本仓库的存在有异议,请在本仓库提 Issue 或直接联系我,我会立即删除并停止传播。

---

## 一、当前仓库的来源链

```
kijai/ComfyUI-llama-cpp                            ← 最原始仓库(作者 kijai)
   └─ lihaoyun6/ComfyUI-llama-cpp_vlm              ← lihaoyun6
         └─ 本仓库(基于 lihaoyun6 的 c3a6314 快照)
```

- **上游(本仓库的直接来源)**:[lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm)
- **上游的上游(原始作者)**:[kijai/ComfyUI-llama-cpp](https://github.com/kijai/ComfyUI-llama-cpp)
- **快照版本**:commit `c3a6314`(2026-06-13，提交信息 "fix some issue")

本仓库不跟随上游更新，为个人记录，保存当时可用状态及少量本地适配。

---

## 二、对比原仓库

### 1. nodes.py 第 546 行:兼容 llama-cpp-python 0.3.45

**原代码**:用 `hasattr(chat_handler, "clip_model_path")` 判断模型是否带 mmproj 视觉模块。

llama-cpp-python 0.3.45 把该属性重构为 `mmproj_path`(`clip_model_path` 成为废弃别名)，导致该判断永远返回 `False`。即使 mmproj 已正确加载,也会误报:
`Image input detected, but the loaded model is not configured with a mmproj module.`

**修改**:改为检查 `mmproj_path`:

```python
if not hasattr(LLAMA_CPP_STORAGE.chat_handler, "mmproj_path") or LLAMA_CPP_STORAGE.chat_handler.mmproj_path is None:
```

### 2. 注意事项

问题的起点是提示词反推全程跑 CPU、显卡 0 占用,一次要 170 秒。排查后分两步解决:

**1. 换底层包,解决 GPU 不占用**

根因是 `llama-cpp-python` 装了 cu128 构建,它的 `ggml-cuda.dll` 需要 CUDA 12 运行时,而机器上没有 CUDA 12(只有 torch 2.9.1+cu130 自带、驱动到 CUDA 13.3),导致 GPU backend 静默失败、自动回退 CPU。采用方案 C:从 JamePeng 的 releases 下载 `v0.3.45-cu130-win-20260731`(cp313,296MB),用 `pip install --force-reinstall --no-deps` 把包从 0.3.40(cu128)换成 0.3.45(cu130),与 torch 共用一套 CUDA 13 runtime。验证成功:`backend registry count: 2`(CUDA+CPU)、`37/37 layers` 全部进 GPU、KV cache 也在 CUDA0,显卡占用问题根除。

**2. 手改一行节点代码,解决 mmproj 误报**

换装后实际跑工作流又报 `Image input detected, but the loaded model is not configured with a mmproj module.`。根因是 0.3.45 重构了 `Qwen3VLChatHandler`:构造参数 `clip_model_path` 被当作废弃别名弹出、内部改存为 `mmproj_path`,新对象上根本没有 `clip_model_path` 属性;而节点 `nodes.py` 第 546 行仍用 `hasattr(..., "clip_model_path")` 判断,永远返回 False,即使 mmproj 已正常加载。修法是把该行属性名改成 `mmproj_path`,重启 ComfyUI。

### 3. README 重写(即本文件)

原 README 已改名保留为 [`README_original.md`](./README_original.md) 和 [`README_original_zh.md`](./README_original_zh.md),内容未做改动。

---

## 三、版权与合规说明

- 本仓库的所有代码、图片、文本,知识产权归**原作者 lihaoyun6 及其上游贡献者**所有,本仓库仅作个人备份和分享用途。
- 上游仓库(lihaoyun6 / kijai)均**未附带开源许可证(LICENSE)**,因此本 fork 不声称获得任何明确的再分发授权;请勿将本仓库用于商业用途。
- 如果您是原作者,不希望此 fork 存在,请告知,我将立即删除。
- 使用本节点造成的任何问题,与本仓库及上游作者无关,请以官方仓库为准。

---

## 四、安装与使用

参见仓库中的README_original.md和README_original_zh.md

---

## 致谢(原作者)

- [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm) @lihaoyun6
- [kijai/ComfyUI-llama-cpp](https://github.com/kijai/ComfyUI-llama-cpp) @kijai
- [llama-cpp-python](https://github.com/JamePeng/llama-cpp-python) @JamePeng
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) @comfyanonymous

本仓库的修改仅为个人环境适配,不涉及对上述项目的任何版权主张。
