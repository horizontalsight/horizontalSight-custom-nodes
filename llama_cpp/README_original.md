# ComfyUI-llama-cpp  
Run LLM/VLM models natively in ComfyUI based on llama.cpp  
**[[📃中文版](./README_zh.md)]** 

## Preview  
![](./img/preview.jpg)

## ⚠️ About this fork (snapshot of lihaoyun6 @ c3a6314)

This repository is a working snapshot of [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm) at commit `c3a6314` (2026-06-13). The code is identical to upstream except for two environment-specific adaptations. It does NOT follow upstream updates.

### 1. nodes.py fix (llama-cpp-python 0.3.45 compatibility)

Upstream checks `hasattr(chat_handler, "clip_model_path")` to verify a VLM model has the mmproj module. llama-cpp-python 0.3.45 renamed this attribute to `mmproj_path` (the old one became a deprecated alias), so the check always returns `False` and falsely raises:

```
Image input detected, but the loaded model is not configured with a mmproj module.
```

This fork changes line 546 of `nodes.py` to check `mmproj_path` instead:

```python
if not hasattr(LLAMA_CPP_STORAGE.chat_handler, "mmproj_path") or LLAMA_CPP_STORAGE.chat_handler.mmproj_path is None:
```

### 2. CUDA 13 (cu130) requirement

Upstream `requirements.txt` installs the **cu128** (CUDA 12.8) build of llama-cpp-python. On machines that only have a CUDA 13 runtime (e.g. torch `2.9.1+cu130`, driver CUDA 13+), the cu128 `ggml-cuda.dll` silently falls back to CPU because the CUDA 12 runtime DLLs are missing — prompt reversal runs on CPU with 0% GPU usage.

Fix: install the **cu130** build. Example for Python 3.13 / Windows:

```powershell
cd "your-ComfyUI-folder"
Invoke-WebRequest -Uri "https://github.com/JamePeng/llama-cpp-python/releases/download/v0.3.45-cu130-win-20260731/llama_cpp_python-0.3.45+cu130-cp313-cp313-win_amd64.whl" -OutFile "llama_cpp_python-0.3.45+cu130-cp313-cp313-win_amd64.whl"
.\python\python.exe -m pip install --force-reinstall --no-deps ".\llama_cpp_python-0.3.45+cu130-cp313-cp313-win_amd64.whl"
```

> `cp313` is for Python 3.13 — adjust to your Python version. cu130 wheels are on [JamePeng/llama-cpp-python Releases](https://github.com/JamePeng/llama-cpp-python/releases).

Verify GPU is actually used (success markers in the log: `loaded CUDA backend`, `backend registry count: 2`, `offloaded 37/37 layers to GPU`):

```powershell
.\python\python.exe -c "import torch; from llama_cpp import Llama; m=Llama(model_path=r'ComfyUI\models\LLM\<your-model>.gguf', n_gpu_layers=-1, n_ctx=512, verbose=True)"
```

### 3. Notes

- **Do NOT update this node via ComfyUI-Manager** — it reinstalls llama-cpp-python as cu128 (GPU silently falls back to CPU again) and overwrites the `nodes.py` fix.
- This fork is adapted for a specific local environment. For new features / new model support, use the [upstream repository](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm).

## Installation  

#### Install the node:  
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/lihaoyun6/ComfyUI-llama-cpp.git
python -m pip install -r ComfyUI-llama-cpp/requirements.txt
```

#### Download models:  
- Place your model files in the `ComfyUI/models/LLM` folder.  

	> If you need a VLM model to process image input, don't forget to download the `mmproj` weights.

## Credits  
- [llama-cpp-python](https://github.com/JamePeng/llama-cpp-python) @JamePeng  
- [ComfyUI-llama-cpp](https://github.com/kijai/ComfyUI-llama-cpp) @kijai  
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) @comfyanonymous
