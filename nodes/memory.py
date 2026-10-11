import gc
import logging
import os
import sys

import comfy.model_management


def find_seedvr2_cache():
    """Locate SeedVR2's live global cache singleton.

    ComfyUI registers each custom node package under a path-derived module name, so the
    package cannot be imported by name here: that would build a second, empty singleton.
    """
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None) or ""
        if os.path.basename(path) == "model_cache.py" and "seedvr2_videoupscaler" in path:
            return module.get_global_cache()
    return None


def release_seedvr2_models():
    cache = find_seedvr2_cache()
    if cache is None:
        return 0

    # The cache only offers per-node removal, so read its entry maps to enumerate them.
    released = 0
    for node_id in list(cache._dit_models):
        released += cache.remove_dit({"node_id": node_id})
    for node_id in list(cache._vae_models):
        released += cache.remove_vae({"node_id": node_id})
    return released


class ClearMemory:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"anything": ("*",)}}

    RETURN_TYPES = ("*",)
    RETURN_NAMES = ("anything",)
    FUNCTION = "clear"
    OUTPUT_NODE = True
    CATEGORY = "utils"
    DESCRIPTION = "Unloads every model from VRAM, including SeedVR2's cached DiT/VAE models. Passes its input through, so run it at the end of a workflow."
    SEARCH_ALIASES = ["clear memory", "unload models", "vram", "清理显存", "卸载模型", "释放显存"]

    def clear(self, anything):
        released = release_seedvr2_models()
        comfy.model_management.unload_all_models()
        gc.collect()
        comfy.model_management.soft_empty_cache()

        message = f"released {released} cached SeedVR2 model(s)" if released else "no cached SeedVR2 models"
        logging.info(f"[ClearMemory] {message}")
        return {"ui": {"text": [message]}, "result": (anything,)}


NODE_CLASS_MAPPINGS = {"ClearMemory": ClearMemory}

NODE_DISPLAY_NAME_MAPPINGS = {"ClearMemory": "清理显存 (Clear Memory)"}
