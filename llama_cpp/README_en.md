# ComfyUI-llama-cpp (personal fork)

> **[[中文版](./README.md)]** · personal fork / mirror

> **This repository is a third-party fork, NOT the original author's work.**

This repository is a derived copy of [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm), based on its commit `c3a6314` (2026-06-13). The code, images and docs come from the original author, and the copyright belongs to the original author and their contributors; this repository is for personal use and backup only.

If the original author or any rights holder objects to this repository's existence, please open an issue or contact me directly, and I will remove it immediately.

---

## I. Source chain of this repository

```
kijai/ComfyUI-llama-cpp                  ← original base repo (author kijai)
   └─ lihaoyun6/ComfyUI-llama-cpp_vlm    ← lihaoyun6
         └─ this repo (snapshot of lihaoyun6 @ c3a6314)
```

- **Upstream (direct source of this repo)**: [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm)
- **Upstream's upstream (original author)**: [kijai/ComfyUI-llama-cpp](https://github.com/kijai/ComfyUI-llama-cpp)
- **Snapshot version**: commit `c3a6314` (2026-06-13, commit message "fix some issue")

This repo does not follow upstream updates; it is a personal record, keeping the working state at that time plus a few local adaptations.

---

## II. Comparison with the original repo

### 1. `nodes.py` line 546: compatibility with llama-cpp-python 0.3.45

**Original code**: used `hasattr(chat_handler, "clip_model_path")` to check whether the model has a mmproj vision module.

llama-cpp-python 0.3.45 refactored this attribute to `mmproj_path` (`clip_model_path` became a deprecated alias), so the check always returns `False`. Even when mmproj is correctly loaded, it falsely raises:
`Image input detected, but the loaded model is not configured with a mmproj module.`

**Fix**: changed to check `mmproj_path`:

```python
if not hasattr(LLAMA_CPP_STORAGE.chat_handler, "mmproj_path") or LLAMA_CPP_STORAGE.chat_handler.mmproj_path is None:
```

### 2. Notes

The starting point of the problem was that prompt reversal ran entirely on CPU with 0% GPU usage, taking 170 seconds per run. After investigation, it was fixed in two steps:

**Step 1: Replace the underlying package to fix GPU not being used**

Root cause: `llama-cpp-python` was installed as the cu128 build; its `ggml-cuda.dll` requires the CUDA 12 runtime, but the machine has no CUDA 12 (only the bundled torch 2.9.1+cu130, driver up to CUDA 13.3), causing the GPU backend to silently fail and fall back to CPU. Adopted Plan C: downloaded `v0.3.45-cu130-win-20260731` (cp313, 296MB) from JamePeng's releases, and used `pip install --force-reinstall --no-deps` to replace the package from 0.3.40 (cu128) to 0.3.45 (cu130), sharing the same CUDA 13 runtime with torch. Verified successfully: `backend registry count: 2` (CUDA+CPU), `37/37 layers` all offloaded to GPU, KV cache also on CUDA0 — the 0% GPU usage issue is gone.

**Step 2: Manually change one line of node code to fix the mmproj false positive**

After the swap, actually running the workflow raised `Image input detected, but the loaded model is not configured with a mmproj module.` again. Root cause: 0.3.45 refactored `Qwen3VLChatHandler` — the constructor argument `clip_model_path` is popped as a deprecated alias and internally stored as `mmproj_path`, so the new object has no `clip_model_path` attribute at all. But line 546 of the node's `nodes.py` still checks `hasattr(..., "clip_model_path")`, which always returns `False` even when mmproj is loaded properly. The fix was to change the attribute name on that line to `mmproj_path` and restart ComfyUI.

### 3. README rewrite (this file)

The original README files are kept unchanged as [`README_original.md`](./README_original.md) and [`README_original_zh.md`](./README_original_zh.md).

---

## III. Copyright & compliance

- All code, images and text in this repository belong to the original author **lihaoyun6** and their upstream contributors; this repository is for personal backup and sharing only.
- Neither upstream repository (lihaoyun6 / kijai) ships an open-source license (no LICENSE), so this fork does not claim any explicit redistribution permission; please do not use it commercially.
- If you are the original author and do not want this fork to exist, please let me know and I will delete it immediately.
- Any issues caused by using this node are not the responsibility of this repository or the upstream authors; please refer to the official repository.

---

## IV. Installation & usage

See `README_original.md` and `README_original_zh.md` in this repository.

---

## Credits (original authors)

- [lihaoyun6/ComfyUI-llama-cpp_vlm](https://github.com/lihaoyun6/ComfyUI-llama-cpp_vlm) @lihaoyun6
- [kijai/ComfyUI-llama-cpp](https://github.com/kijai/ComfyUI-llama-cpp) @kijai
- [llama-cpp-python](https://github.com/JamePeng/llama-cpp-python) @JamePeng
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) @comfyanonymous

The modifications in this repository are only local-environment adaptations and do not claim any copyright over the projects above.
