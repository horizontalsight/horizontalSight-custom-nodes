# -*- coding: utf-8 -*-
"""horizontalSight-custom-nodes —— 伞包（facade）

把原来分散的 5 个 horizontalSight 包合并成一个，内部按功能分文件夹：

    nodes/image_folder.py     LoadImagesFromFolder / SaveImagesWithPrompt
    nodes/sequence_save.py    SequenceImageSave
    nodes/memory.py           ClearMemory
    llama_cpp/                上游 git 仓库（整包搬入，**未改动一个字节**，git pull 仍可用）
    prestartup_script.py      启动期脚本（**必须留在包根**：ComfyUI 只扫描
                              custom_nodes/<包名>/prestartup_script.py 这一层）

本文件只做一件事：把各子模块的 NODE_CLASS_MAPPINGS 汇总起来。

⚠️ 节点**类型名**与拆分前完全一致，所以既有工作流（Krea2 系列的 SequenceImageSave、
   批量反推里的 llama_cpp_* / LoadImagesFromFolder / SaveImagesWithPrompt 等）
   不需要任何改动。子模块之间没有重名，且下面有冲突保护，不会静默覆盖。
"""

import importlib
import logging

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# 记录加载结果，方便在启动日志里一眼看出哪个子模块出了问题
_LOADED = []


def _merge(label, module):
    classes = getattr(module, "NODE_CLASS_MAPPINGS", None) or {}
    display = getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", None) or {}
    added = 0
    for node_type, cls in classes.items():
        if node_type in NODE_CLASS_MAPPINGS:
            logging.error(
                "[horizontalSight] 节点类型名冲突: %s（来自 %s），已保留先加载的那个，"
                "请检查是否重复打包", node_type, label,
            )
            continue
        NODE_CLASS_MAPPINGS[node_type] = cls
        NODE_DISPLAY_NAME_MAPPINGS[node_type] = display.get(node_type, node_type)
        added += 1
    _LOADED.append(f"{label}={added}")
    return added


def _load(label, dotted):
    """单独加载一个子模块；任何一个失败都不影响其余节点。"""
    try:
        module = importlib.import_module(dotted, __package__)
    except Exception as exc:                      # noqa: BLE001 — 必须隔离，否则整包失效
        logging.error("[horizontalSight] 子模块 %s 加载失败: %s: %s",
                      label, type(exc).__name__, exc)
        _LOADED.append(f"{label}=FAILED")
        return
    _merge(label, module)


# 纯本地节点（原先各自是独立包）
for _name in ("image_folder", "sequence_save", "memory", "booru_tags"):
    _load(f"nodes.{_name}", f".nodes.{_name}")

# 上游 git 仓库，整体搬入 llama_cpp/ 子目录
_load("llama_cpp", ".llama_cpp")

logging.info("[horizontalSight] 伞包已加载: %s | 节点总数 %d",
             ", ".join(_LOADED), len(NODE_CLASS_MAPPINGS))

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
