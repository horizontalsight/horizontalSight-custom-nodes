# ComfyUI-llama-cpp
在 ComfyUI 中基于 llama.cpp 框架原生运行 LLM & VLM 模型。  
**[[📃English](./README.md)]**   

## 预览
![](./img/preview.jpg) 

## ⚠️ 本分支说明(Fork 自 lihaoyun6 @ c3a6314)

本仓库是 [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm) 在 commit `c3a6314`(2026-06-13)的可用版本快照,代码主体与原仓库一致,仅做了两处与本地环境相关的适配,未跟随上游后续更新。

### 1. nodes.py 修改(llama-cpp-python 0.3.45 兼容)

上游代码在图片输入检查里用 `hasattr(chat_handler, "clip_model_path")` 判断模型是否带有 mmproj 视觉模块。llama-cpp-python 0.3.45 把多模态 handler 的属性从 `clip_model_path` 重构为 `mmproj_path`(前者成为废弃别名),导致该判断永远返回 `False`——即使 mmproj 已正确加载(显存正常占用),也会误报:

```
Image input detected, but the loaded model is not configured with a mmproj module.
```

本分支已将 `nodes.py` 第 546 行改为检查 `mmproj_path`:

```python
if not hasattr(LLAMA_CPP_STORAGE.chat_handler, "mmproj_path") or LLAMA_CPP_STORAGE.chat_handler.mmproj_path is None:
```

### 2. CUDA 13(cu130)依赖说明

上游 `requirements.txt` 默认安装的是 **cu128**(CUDA 12.8)构建的 llama-cpp-python。如果你的机器没有 CUDA 12 运行时(例如 torch 为 `2.9.1+cu130`、驱动为 CUDA 13+),cu128 的 `ggml-cuda.dll` 会因缺少 CUDA 12 的 DLL 而**静默回退 CPU**,表现为提示词反推跑 CPU、GPU 占用为 0。

解决办法:安装 **cu130**(CUDA 13.0)构建。Python 3.13 / Windows 示例:

```powershell
cd "你的ComfyUI目录"
Invoke-WebRequest -Uri "https://github.com/JamePeng/llama-cpp-python/releases/download/v0.3.45-cu130-win-20260731/llama_cpp_python-0.3.45+cu130-cp313-cp313-win_amd64.whl" -OutFile "llama_cpp_python-0.3.45+cu130-cp313-cp313-win_amd64.whl"
.\python\python.exe -m pip install --force-reinstall --no-deps ".\llama_cpp_python-0.3.45+cu130-cp313-cp313-win_amd64.whl"
```

> `cp313` 对应 Python 3.13,请按你的 Python 版本替换;cu130 的 wheel 可在 [JamePeng/llama-cpp-python Releases](https://github.com/JamePeng/llama-cpp-python/releases) 找到。

验证 GPU 是否生效(成功标志:日志出现 `loaded CUDA backend`、`backend registry count: 2`、`offloaded 37/37 layers to GPU`):

```powershell
.\python\python.exe -c "import torch; from llama_cpp import Llama; m=Llama(model_path=r'ComfyUI\models\LLM\<你的模型>.gguf', n_gpu_layers=-1, n_ctx=512, verbose=True)"
```

### 3. 注意事项

- **不要用 ComfyUI-Manager 更新本节点**:它会按 requirements.txt 把 llama-cpp-python 重新装回 cu128 版,GPU 会再次失效并静默回退 CPU,同时会覆盖 `nodes.py` 的修复。
- 本分支为个人环境适配版本,如需上游新功能/新模型支持,请使用 [上游原仓库](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm)。

## 安装步骤

#### 安装节点:
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/lihaoyun6/ComfyUI-llama-cpp.git
python -m pip install -r ComfyUI-llama-cpp/requirements.txt
```

### 模型路径:
- 请将下载的 `.gguf` 模型放置在 `ComfyUI/models/LLM` 目录中.  

	> 在使用VLM模型进行图像推理之前, 请确保已经下载并选择了主模型对应的`mmproj`权重文件.

## 致谢
- [llama-cpp-python](https://github.com/JamePeng/llama-cpp-python) @JamePeng  
- [ComfyUI-llama-cpp](https://github.com/kijai/ComfyUI-llama-cpp) @kijai
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) @comfyanonymous
