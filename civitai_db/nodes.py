# -*- coding: utf-8 -*-
"""civitai_db 的 5 个 ComfyUI 节点。"""

from __future__ import annotations

import json
import threading

try:
    from . import api, core, inject
except ImportError:  # 允许在包外单独 import（单元测试）
    import api
    import core
    import inject

try:
    import comfy.model_management as model_management
    import comfy.utils as comfy_utils
except Exception:  # pragma: no cover - ComfyUI 之外
    model_management = None
    comfy_utils = None


def _service():
    return api.get_service()


def _cancel_checker(event):
    def is_cancelled():
        if event.is_set():
            return True
        if model_management is not None:
            probe = getattr(model_management, "processing_interrupted", None)
            if probe is not None:
                try:
                    return bool(probe())
                except Exception:
                    return event.is_set()
        return False
    return is_cancelled


def _progress_bar():
    state = {"bar": None}
    bar_cls = getattr(comfy_utils, "ProgressBar", None) if comfy_utils else None

    def on_progress(done, total, detail=""):
        if bar_cls is None or not total:
            return
        if state["bar"] is None:
            state["bar"] = bar_cls(total)
        state["bar"].update_absolute(done, total)

    return on_progress


ACTION_MAP = {
    "扫描登记": "scan",
    "校验路径": "verify",
    "增量哈希": "hash_inc",
    "全量哈希": "hash_full",
    "联网查询（新增）": "query_new",
    "重查未找到": "query_not_found",
    "重试失败": "query_failed",
}


class CivitaiDBPanel:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "db_path": ("STRING", {"default": "", "multiline": False,
                                       "tooltip": "留空使用 civitai_db 的配置"}),
            },
        }

    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("summary", "count")
    FUNCTION = "run"
    CATEGORY = "civitai"
    OUTPUT_NODE = True
    DESCRIPTION = "打开 Civitai 数据库管理面板（按钮在节点上）。"

    def run(self, db_path=""):
        state = _service().snapshot()
        stats = state["stats"]
        summary = ("数据库 {total} 条 · 已收录 {ok} · 待查询 {new} · "
                   "未找到 {not_found} · 失败 {error}").format(**stats)
        return (summary, stats["total"])


class CivitaiModelLookup:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": "", "multiline": False,
                                     "tooltip": "文件名、相对路径或绝对路径"}),
                "auto_hash": ("BOOLEAN", {"default": False,
                                          "tooltip": "找不到记录时，对本地文件计算 SHA256 再查库"}),
                "allow_network": ("BOOLEAN", {"default": False,
                                              "tooltip": "数据库没有时兜底联网查询（不写库）"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("air", "model_name", "status", "detail")
    FUNCTION = "run"
    CATEGORY = "civitai"
    DESCRIPTION = "按文件名/路径查询本地库里的 Civitai AIR，只读。"

    def run(self, model, auto_hash, allow_network):
        result = _service().lookup(model, auto_hash=auto_hash,
                                   allow_network=allow_network)
        detail = {k: v for k, v in result.items() if k != "entry"}
        return (result.get("air") or "", result.get("model_name") or "",
                result.get("status") or "", json.dumps(detail, ensure_ascii=False))


class CivitaiDBUpdate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "action": (list(ACTION_MAP.keys()),),
                "folder": ("STRING", {"default": "", "multiline": False,
                                      "tooltip": "扫描目录；留空则用配置里的 ComfyUI 类别目录"}),
                "extensions": ("STRING", {"default": "", "multiline": False,
                                          "tooltip": "本次扫描的后缀，逗号分隔；留空用配置"}),
                "limit": ("INT", {"default": 0, "min": 0, "max": 100000,
                                  "tooltip": "联网查询上限；0 = 不限"}),
                "force": ("BOOLEAN", {"default": False,
                                      "tooltip": "全量哈希时忽略缓存"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("summary", "stats_json")
    FUNCTION = "run"
    CATEGORY = "civitai"
    OUTPUT_NODE = True
    DESCRIPTION = "在队列里执行一次数据库维护：扫描/校验/哈希/联网查询。"

    def run(self, action, folder, extensions, limit, force):
        svc = _service()
        if svc.jobs.is_running():
            raise RuntimeError("已有维护任务在运行，请先等待或取消。")
        action_id = ACTION_MAP[action]
        params = {"folder": folder.strip(), "limit": int(limit), "force": bool(force)}
        if extensions.strip():
            params["extensions"] = extensions.strip()
        cancel_event = threading.Event()
        logs = []

        def log(message):
            logs.append(str(message))
            print("[civitai_db] {}".format(message))

        try:
            result = svc.execute(action_id, params, log=log,
                                 progress=_progress_bar(),
                                 cancel=_cancel_checker(cancel_event))
        except core.Cancelled:
            raise RuntimeError("已取消")
        stats = json.dumps(svc.snapshot()["stats"], ensure_ascii=False)
        summary = json.dumps(result, ensure_ascii=False)
        if len(summary) > 4000:
            summary = summary[:4000] + "…"
        return (summary, stats)


class CivitaiAirsFromWorkflow:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "kinds": ("STRING", {"default": "model,lora", "multiline": False,
                                     "tooltip": "model,lora / all / model,lora,vae,…"}),
                "include_disabled": ("BOOLEAN", {"default": False}),
                "allow_network": ("BOOLEAN", {"default": False}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "INT")
    RETURN_NAMES = ("airs", "resolved_json", "unresolved", "count")
    FUNCTION = "run"
    CATEGORY = "civitai"
    DESCRIPTION = "从当前工作流提取模型并输出 AIR 列表，不写图片、不写数据库。"

    def run(self, kinds, include_disabled, allow_network, prompt=None,
            extra_pnginfo=None):
        svc = _service()
        workflow = None
        if isinstance(extra_pnginfo, dict):
            workflow = extra_pnginfo.get("workflow")
        resources = inject.extract_resources(prompt, workflow, include_disabled)
        if not resources:
            return ("", "[]", "", 0)
        resolved, unresolved, _dropped = inject._resolve_resources(
            resources, svc.model_index(), svc.database.db, svc.hash_cache(),
            kinds=kinds, allow_api=allow_network,
            api_base=svc.config.get("api_base"),
            timeout=int(svc.config.get("request_timeout") or 20),
            retries=int(svc.config.get("query_retries") or 3))
        resolved.sort(key=lambda r: inject.KIND_ORDER.get(r["kind"], 5))
        airs = list(dict.fromkeys(r["air"] for r in resolved if r.get("air")))
        unresolved_text = "\n".join("{} -> {}".format(n, why)
                                    for n, why in unresolved)
        return ("\n".join(airs),
                json.dumps(resolved, ensure_ascii=False),
                unresolved_text, len(airs))


class CivitaiInjectImages:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "input_folder": ("STRING", {"default": "", "multiline": False}),
                "output_folder": ("STRING", {"default": "", "multiline": False,
                                             "tooltip": "留空 = <输入目录>/add_data"}),
                "suffix": ("STRING", {"default": "_comfy"}),
                "kinds": ("STRING", {"default": "model,lora"}),
                "dry_run": ("BOOLEAN", {"default": True,
                                        "tooltip": "只预览，不写文件"}),
                "overwrite": ("BOOLEAN", {"default": False}),
                "include_disabled": ("BOOLEAN", {"default": False}),
                "allow_api": ("BOOLEAN", {"default": False}),
                "fix_prompt": ("BOOLEAN", {"default": True}),
                "fix_values": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("summary", "unresolved", "report_path")
    FUNCTION = "run"
    CATEGORY = "civitai"
    OUTPUT_NODE = True
    DESCRIPTION = "批量给文件夹里的 PNG 注入 AIR，并修复 Civitai 提示词。"

    def run(self, input_folder, output_folder, suffix, kinds, dry_run,
            overwrite, include_disabled, allow_api, fix_prompt, fix_values):
        svc = _service()
        if svc.jobs.is_running():
            raise RuntimeError("已有维护任务在运行，请先等待或取消。")
        cancel_event = threading.Event()
        logs = []

        def log(message):
            logs.append(str(message))
            print("[civitai_db] {}".format(message))

        params = {
            "input": input_folder.strip(),
            "output": output_folder.strip(),
            "suffix": suffix,
            "kinds": kinds,
            "dry_run": bool(dry_run),
            "overwrite": bool(overwrite),
            "include_disabled": bool(include_disabled),
            "allow_api": bool(allow_api),
            "fix_prompt": bool(fix_prompt),
            "fix_values": bool(fix_values),
        }
        try:
            result = svc.execute("inject", params, log=log,
                                 progress=_progress_bar(),
                                 cancel=_cancel_checker(cancel_event))
        except core.Cancelled:
            raise RuntimeError("已取消")
        summary = {k: result.get(k) for k in
                   ("total", "ok", "skipped", "errors", "prompt_fixed")}
        unresolved = "\n".join("{} -> {}".format(f, n, why)
                               for f, n, why in result.get("unresolved", []))
        # 不在输出目录写任何 JSON 报告；第三个输出保留为空字符串以兼容旧工作流
        return (json.dumps(summary, ensure_ascii=False), unresolved, "")


NODE_CLASS_MAPPINGS = {
    "CivitaiDBPanel": CivitaiDBPanel,
    "CivitaiModelLookup": CivitaiModelLookup,
    "CivitaiDBUpdate": CivitaiDBUpdate,
    "CivitaiAirsFromWorkflow": CivitaiAirsFromWorkflow,
    "CivitaiInjectImages": CivitaiInjectImages,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CivitaiDBPanel": "Civitai 数据库面板",
    "CivitaiModelLookup": "查询 Civitai AIR",
    "CivitaiDBUpdate": "Civitai 数据库维护",
    "CivitaiAirsFromWorkflow": "从工作流提取 AIR",
    "CivitaiInjectImages": "Civitai PNG 注入",
}

# 路由在节点模块加载时注册；没有 ComfyUI 时自动跳过
try:
    api.register()
except Exception as exc:  # pragma: no cover
    print("[civitai_db] 路由注册失败：{}".format(exc))
