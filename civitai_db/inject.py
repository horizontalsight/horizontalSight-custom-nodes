# -*- coding: utf-8 -*-
"""PNG 元数据注入与 Civitai 提示词修复。

本模块把 ``civitai_ckpt_inject/inject_civitai_airs.py`` 的能力重新实现到
``civitai_db`` 下，不再依赖旧项目：

* 读取 PNG 里内嵌的 ``prompt`` / ``workflow``；
* 提取 ckpt / LoRA 等模型名；
* 模型名 -> 本地文件 -> SHA256 -> AutoV2 -> 本地库 -> AIR；
* 把 AIR 写入 ``workflow.extra.airs``；
* 修复 Civitai 读不出来的提示词，并把连线数值内联成字面量；
* 只改 ``prompt`` / ``workflow`` 两个块，其余块逐字节保留。

与旧脚本的差异：
* 模型索引由调用方传入（ComfyUI 类别根目录），不再假设单个 models 根；
* 哈希缓存、数据库、API 地址由调用方配置；
* 支持 cancel / progress / log 回调；
* 不自动写数据库；联网兜底只用于本次解析。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import struct
import zlib

try:
    from . import core
except ImportError:  # 允许在包外单独 import（单元测试）
    import core

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MODEL_EXTS = core.BROAD_MODEL_EXTENSIONS

# 输入里代表“模型文件名”的键名
NAME_KEYS = {
    "ckpt_name", "lora_name", "unet_name", "model_name", "vae_name",
    "clip_name", "clip_name1", "clip_name2", "clip_name3", "clip_name4",
    "control_net_name", "style_model_name", "gligen_name", "gguf_name",
    "diffusion_model", "text_encoder", "upscale_model_name", "model_path",
    "model", "patch_name", "dit_model",
}

# “节点角色”分组。不能按 Civitai 返回的 AIR kind 过滤：
# qwen_image_vae 在 Civitai 上以 Checkpoint 发布，qwen3vl_4b 被发布成
# krea2 的 diffusionmodel；按 AIR kind 会误收，必须按节点角色过滤。
KIND_GROUPS = {
    "model": {"checkpoint", "diffusionmodel"},
    "lora": {"lora"},
    "controlnet": {"controlnet"},
    "vae": {"vae"},
    "textencoder": {"textencoder"},
    "upscaler": {"upscaler"},
}
DEFAULT_KINDS = "model,lora"
KIND_ORDER = {"checkpoint": 0, "diffusionmodel": 0, "lora": 1,
              "controlnet": 2}

IGNORE_HINTS = (
    "llama_cpp", "llamacpp", "tipo", "preprocessor", "depthanything",
    "sam3", "mmproj", "prompt_enhancer", "promptenhancer",
)

SAMPLER_CLASSES = {
    "KSampler", "KSamplerAdvanced", "KSampler (Efficient)",
    "KSampler //Inspire", "KSampler (Efficient) //Inspire",
    "SamplerCustomAdvanced",
}

POS_TEXT_KEYS = ("text", "text_positive", "positive_prompt", "positive",
                 "prompt", "populated_text", "text_g", "value", "string")
NEG_TEXT_KEYS = ("text_negative", "negative_prompt", "negative", "text_l",
                 "text", "populated_text", "prompt", "value", "string")

NUMERIC_KEYS = ("steps", "cfg", "denoise", "seed", "noise_seed", "width",
                "height", "batch_size", "start_at_step", "end_at_step",
                "strength", "strength_model", "strength_clip",
                "start_percent", "end_percent")
NUMERIC_SOURCE_KEYS = ("value", "Value", "int", "Int", "seed", "noise_seed",
                       "float", "Float", "number", "steps")


# ---------------------------------------------------------------------------
# PNG 块读写（纯字节，不重新编码图像）
# ---------------------------------------------------------------------------
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def parse_png(data):
    """返回 [[块类型, 负载], ...]，保持原始顺序。"""
    if data[:8] != PNG_SIG:
        raise ValueError("不是 PNG 文件")
    pos, chunks = 8, []
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if len(payload) != length:
            raise ValueError("块 {} 被截断".format(ctype))
        chunks.append([ctype, payload])
        pos += 12 + length
    return chunks


def build_png(chunks):
    out = bytearray(PNG_SIG)
    for ctype, payload in chunks:
        out += struct.pack(">I", len(payload))
        out += ctype
        out += payload
        out += struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
    return bytes(out)


def decode_text_chunk(ctype, payload):
    """返回 (关键字, 文本, 原始是否 zlib 压缩)。"""
    if ctype == b"tEXt":
        key, _, val = payload.partition(b"\x00")
        return key.decode("latin-1"), val.decode("latin-1"), False
    if ctype == b"zTXt":
        key, _, rest = payload.partition(b"\x00")
        return key.decode("latin-1"), zlib.decompress(rest[1:]).decode("latin-1"), True
    if ctype == b"iTXt":
        key, rest = payload.split(b"\x00", 1)
        compressed = rest[0] == 1
        rest = rest[2:]
        _lang, rest = rest.split(b"\x00", 1)
        _trans, text = rest.split(b"\x00", 1)
        if text.endswith(b"\x00"):
            text = text[:-1]
        if compressed:
            text = zlib.decompress(text)
        return key.decode("latin-1"), text.decode("utf-8"), compressed
    raise ValueError("不支持的文本块 {}".format(ctype))


def encode_text_chunk(ctype, key, text, compressed):
    if ctype == b"tEXt":
        return key.encode("latin-1") + b"\x00" + text.encode("latin-1")
    if ctype == b"zTXt":
        return (key.encode("latin-1") + b"\x00\x00"
                + zlib.compress(text.encode("latin-1"), 9))
    if ctype == b"iTXt":
        raw = text.encode("utf-8")
        flag, method = (1, 0) if compressed else (0, 0)
        body = zlib.compress(raw, 9) if compressed else raw
        return (key.encode("latin-1") + b"\x00" + bytes([flag, method])
                + b"\x00" + b"\x00" + body)
    raise ValueError("不支持的文本块 {}".format(ctype))


def collect_text(chunks):
    """返回 {关键字: (块类型, 文本, 是否压缩, 块下标)}。"""
    found = {}
    for i, (ctype, payload) in enumerate(chunks):
        if ctype in (b"tEXt", b"zTXt", b"iTXt"):
            try:
                key, text, comp = decode_text_chunk(ctype, payload)
            except Exception:
                continue
            found[key] = (ctype, text, comp, i)
    return found


# ---------------------------------------------------------------------------
# 从工作流里找出用到的模型
# ---------------------------------------------------------------------------
def _is_model_file(value):
    return isinstance(value, str) and value.lower().endswith(MODEL_EXTS)


def _kind_from_class(class_type):
    c = (class_type or "").lower()
    if any(h in c for h in IGNORE_HINTS):
        return "other"
    if "lora" in c:
        return "lora"
    if "modelpatch" in c or "controlnet" in c or "control_net" in c:
        return "controlnet"
    if "checkpoint" in c:
        return "checkpoint"
    if "unet" in c or "diffusion" in c:
        return "diffusionmodel"
    if "vae" in c:
        return "vae"
    if "clip" in c or "text_encoder" in c:
        return "textencoder"
    if "upscale" in c or "esrgan" in c or "seedvr2" in c:
        return "upscaler"
    return "unknown"


def extract_resources(prompt_obj, workflow_obj, include_disabled=False):
    """按“使用顺序”返回 [(原始文件名, 角色), ...]，已去重。

    以 API 格式的 prompt（真实执行图，bypass 节点不在里面）为准；
    没有 prompt 时回退到 UI 格式的 workflow，并按 mode 过滤 bypass 节点。
    """
    seen, ordered = set(), []

    def add(name, kind):
        key = name.replace("/", "\\").lower()
        if key not in seen:
            seen.add(key)
            ordered.append((name, kind))

    if isinstance(prompt_obj, dict):
        for node in prompt_obj.values():
            if not isinstance(node, dict):
                continue
            kind = _kind_from_class(node.get("class_type", ""))
            if kind == "other":
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue
            for key, value in inputs.items():
                # rgthree "Power Lora Loader" 之类：值是 {"on":bool, "lora":name}
                if isinstance(value, dict):
                    if "lora" in value and (include_disabled or value.get("on")) \
                            and _is_model_file(value["lora"]):
                        add(value["lora"], "lora")
                    continue
                if _is_model_file(value) and (key in NAME_KEYS or kind != "unknown"):
                    add(value, kind)
        return ordered

    for node in (workflow_obj or {}).get("nodes", []):
        if not include_disabled and node.get("mode", 0) in (2, 4):
            continue  # 2 = muted, 4 = bypass
        kind = _kind_from_class(node.get("type", ""))
        if kind == "other":
            continue
        widgets = node.get("widgets_values")
        if isinstance(widgets, dict):
            widgets = [widgets]
        if not isinstance(widgets, list):
            continue
        for value in widgets:
            if isinstance(value, dict):
                if "lora" in value and (include_disabled or value.get("on")) \
                        and _is_model_file(value["lora"]):
                    add(value["lora"], "lora")
            elif _is_model_file(value):
                add(value, kind)
    return ordered


# ---------------------------------------------------------------------------
# Civitai 提示词修复
# ---------------------------------------------------------------------------
def _ref_target(prompt_obj, value):
    """["12", 0] -> ("12", 节点字典)；不是节点引用时返回 (None, None)。"""
    if isinstance(value, list) and value and isinstance(value[0], (str, int)):
        nid = str(value[0])
        node = prompt_obj.get(nid)
        if isinstance(node, dict):
            return nid, node
    return None, None


def _lookup_text(prompt_obj, ref, keys, allow_empty=False, follow=False,
                 seen=None, depth=0):
    """沿节点引用找提示词字符串。

    allow_empty 只在最外层生效：负向提示词可能“明确就是空串”。
    follow 只给正向用：允许沿 conditioning 链继续找。
    负向不能 follow —— ConditioningZeroOut 的 conditioning 就是正向文字。
    """
    nid, node = _ref_target(prompt_obj, ref)
    if node is None or depth > 8:
        return ""
    seen = set() if seen is None else seen
    if nid in seen:
        return ""
    seen.add(nid)
    inputs = node.get("inputs") or {}
    for k in keys:
        v = inputs.get(k)
        if isinstance(v, str) and (allow_empty or v.strip()):
            return v
    for k in keys:
        v = inputs.get(k)
        if isinstance(v, list):
            got = _lookup_text(prompt_obj, v, keys, False, follow, seen, depth + 1)
            if got:
                return got
    if follow:
        for k in ("conditioning", "input", "positive", "guider"):
            got = _lookup_text(prompt_obj, inputs.get(k), keys, False,
                               follow, seen, depth + 1)
            if got:
                return got
    return ""


def _lookup_number(prompt_obj, ref, key, seen=None, depth=0):
    """把 ["12", 0] 解析成数字字面量（PrimitiveInt / Seed / rgthree …）。"""
    nid, node = _ref_target(prompt_obj, ref)
    if node is None or depth > 6:
        return None
    seen = set() if seen is None else seen
    if nid in seen:
        return None
    seen.add(nid)
    inputs = node.get("inputs") or {}
    for k in (key,) + NUMERIC_SOURCE_KEYS:
        v = inputs.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str) and v.strip():
            try:
                return int(v)
            except ValueError:
                try:
                    return float(v)
                except ValueError:
                    pass
        elif isinstance(v, list):
            got = _lookup_number(prompt_obj, v, key, seen, depth + 1)
            if got is not None:
                return got
    return None


def inline_numeric_refs(prompt_obj):
    """把 steps / seed / width / height 之类“连了线的数值”内联成字面量。"""
    changed, count = False, 0
    for node in prompt_obj.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for k in NUMERIC_KEYS:
            if not isinstance(inputs.get(k), list):
                continue
            val = _lookup_number(prompt_obj, inputs[k], k)
            if val is not None:
                inputs[k] = val
                changed, count = True, count + 1
    return changed, count


def _sampler_ids(prompt_obj):
    ids = [nid for nid, n in prompt_obj.items()
           if isinstance(n, dict) and n.get("class_type") in SAMPLER_CLASSES]
    if not ids:
        ids = [nid for nid, n in prompt_obj.items()
               if isinstance(n, dict)
               and isinstance((n.get("inputs") or {}).get("positive"), list)
               and isinstance((n.get("inputs") or {}).get("negative"), list)]
    return ids


def _next_node_id(prompt_obj):
    nums = [int(k) for k in prompt_obj if str(k).lstrip("-").isdigit()]
    return str(max(nums) + 1 if nums else 1)


def _fix_prompt_nodes(prompt_obj, report):
    """给采样器的正/负节点补上字面量 inputs.text。"""
    changed = False
    for sid in _sampler_ids(prompt_obj):
        inputs = prompt_obj[sid].get("inputs")
        if not isinstance(inputs, dict):
            continue

        pos_id, pos_node = _ref_target(prompt_obj, inputs.get("positive"))
        if pos_node is None:
            continue
        pos_text = _lookup_text(prompt_obj, inputs.get("positive"), POS_TEXT_KEYS,
                                follow=True)
        pos_inputs = pos_node.setdefault("inputs", {})
        if pos_text and pos_inputs.get("text") != pos_text:
            pos_inputs["text"] = pos_text
            report["added_text"].append(pos_id)
            changed = True
        if pos_text and not report["positive"]:
            report["positive"] = pos_text

        neg_id, neg_node = _ref_target(prompt_obj, inputs.get("negative"))
        if neg_node is None:
            continue
        # 正负共用同一个节点时（TextEncodeQwenImage21 的 0/1 输出），
        # 空串要当成“负向就是空的”，不能顺着找到正向那段文字。
        neg_text = _lookup_text(prompt_obj, inputs.get("negative"), NEG_TEXT_KEYS,
                                allow_empty=(neg_id == pos_id))
        if neg_id != pos_id:
            neg_inputs = neg_node.setdefault("inputs", {})
            if neg_text and neg_inputs.get("text") != neg_text:
                neg_inputs["text"] = neg_text
                report["added_text"].append(neg_id)
                changed = True
        elif neg_text != pos_text:
            # 一个节点没法同时表达两段文字 -> 另做一个标准负向节点
            nid = _next_node_id(prompt_obj)
            new_inputs = {"text": neg_text}
            if pos_inputs.get("clip") is not None:
                new_inputs["clip"] = pos_inputs["clip"]
            prompt_obj[nid] = {"class_type": "CLIPTextEncode",
                               "inputs": new_inputs,
                               "_meta": {"title": "negative (civitai-air-inject)"}}
            inputs["negative"] = [nid, 0]
            report["created_nodes"].append(nid)
            changed = True
        if neg_text and not report["negative"]:
            report["negative"] = neg_text
    return changed


def repair_civitai_prompt(prompt_obj, fix_prompt=True, fix_values=True):
    """就地修复 API 格式的 prompt，返回 (report, changed)。"""
    report = {"positive": "", "negative": "", "added_text": [],
              "created_nodes": [], "inlined": 0, "repaired": False}
    if not isinstance(prompt_obj, dict):
        return report, False

    changed = False
    if fix_values:
        c, n = inline_numeric_refs(prompt_obj)
        report["inlined"] = n
        changed = changed or c
    if fix_prompt:
        c = _fix_prompt_nodes(prompt_obj, report)
        changed = changed or c
    report["repaired"] = bool(report["added_text"] or report["created_nodes"])
    return report, changed


# ---------------------------------------------------------------------------
# 模型 -> AIR
# ---------------------------------------------------------------------------
def _wanted_kinds(kinds):
    if not kinds or kinds.strip().lower() == "all":
        return None
    wanted = set()
    for group in kinds.split(","):
        wanted |= KIND_GROUPS.get(group.strip(), set())
    return wanted


def _kind_allowed(kind, wanted):
    return wanted is None or kind in wanted


def _resolve_resources(resources, index, db, hash_cache, kinds=DEFAULT_KINDS,
                       allow_api=False, api_base=core.API_BASE_DEFAULT,
                       timeout=20, retries=3, cancel=None, log=None):
    """把 (文件名, 角色) 解析成 (resolved, unresolved, dropped)。"""
    wanted = _wanted_kinds(kinds)
    resolved, unresolved, dropped = [], [], []

    for name, kind in resources:
        local = index.find(name, kind) if index else None
        if not local:
            if _kind_allowed(kind, wanted):
                unresolved.append((name, "本地 models 目录里找不到该文件"))
            continue

        try:
            digest = hash_cache.compute(local, cancel=cancel)
        except core.Cancelled:
            raise
        except Exception as exc:
            if _kind_allowed(kind, wanted):
                unresolved.append((name, "计算哈希失败：{}".format(exc)))
            continue

        av = core.autov2(digest)
        entry = db.get(av) if db else None
        error = None
        if not entry:
            error = "本地库没有 AutoV2={}（在「② 更新数据库」里补一条即可）".format(av)
        elif entry.get("failed"):
            if entry.get("failedKind") == "not_found":
                error = "Civitai 未收录（404）：AutoV2={}".format(av)
            else:
                error = "本地库标记为查询失败：{}".format(entry.get("error") or "未知原因")
        elif not entry.get("AIR"):
            error = "本地库有记录但还没有 AIR：AutoV2={}".format(av)

        if (not entry or not entry.get("AIR") or entry.get("failed")) and allow_api:
            api_entry, api_error = core.query_by_autov2(
                av, api_base=api_base, timeout=timeout, retries=retries,
                cancel=cancel)
            if api_entry:
                entry = api_entry
                error = None
            else:
                error = "联网兜底失败：{}".format(
                    (api_error or {}).get("message") or "未知错误")

        if not entry or entry.get("failed") or not entry.get("AIR"):
            if _kind_allowed(kind, wanted):
                unresolved.append((name, error or "没有可用 AIR"))
            continue

        item = {
            "name": name,
            "kind": kind,
            "air": entry.get("AIR"),
            "model": entry.get("modelName") or "",
            "path": local,
            "auto_v2": av,
        }
        if _kind_allowed(kind, wanted):
            resolved.append(item)
        else:
            dropped.append(item)

    return resolved, unresolved, dropped


# ---------------------------------------------------------------------------
# 单张图
# ---------------------------------------------------------------------------
def _process_one(path, out_path, index, db, hash_cache, opts):
    with open(path, "rb") as f:
        original = f.read()
    chunks = parse_png(original)
    texts = collect_text(chunks)

    if "workflow" not in texts:
        return {"status": "skip", "reason": "没有内嵌 workflow"}
    wf_ctype, wf_text, wf_comp, wf_index = texts["workflow"]
    try:
        workflow = json.loads(wf_text)
    except Exception as exc:
        return {"status": "error", "reason": "workflow 不是合法 JSON: {}".format(exc)}
    if not isinstance(workflow, dict):
        return {"status": "error", "reason": "workflow 不是 JSON 对象"}

    prompt_obj = None
    if "prompt" in texts:
        try:
            prompt_obj = json.loads(texts["prompt"][1])
        except Exception:
            prompt_obj = None

    resources = extract_resources(prompt_obj, workflow, opts["include_disabled"])
    if not resources:
        return {"status": "skip", "reason": "工作流里没找到模型引用"}

    resolved, unresolved, dropped = _resolve_resources(
        resources, index, db, hash_cache, kinds=opts["kinds"],
        allow_api=opts["allow_api"], api_base=opts["api_base"],
        timeout=opts["timeout"], retries=opts["retries"],
        cancel=opts["cancel"], log=opts["log"])

    if not resolved:
        return {"status": "skip", "reason": "没有可署名的 ckpt/LoRA",
                "unresolved": unresolved,
                "dropped": [d["name"] for d in dropped]}

    resolved.sort(key=lambda r: KIND_ORDER.get(r["kind"], 5))
    airs = list(dict.fromkeys(r["air"] for r in resolved if r.get("air")))

    # ---------------- 修复 Civitai 提示词 / 数值 ----------------
    prompt_fix = None
    prompt_changed = False
    if prompt_obj is not None and (opts["fix_prompt"] or opts["fix_values"]):
        prompt_fix, prompt_changed = repair_civitai_prompt(
            prompt_obj, fix_prompt=opts["fix_prompt"],
            fix_values=opts["fix_values"])

    extra = workflow.get("extra")
    if not isinstance(extra, dict):
        extra = {}
        workflow["extra"] = extra
    before = list(extra.get("airs") or [])
    if before == airs and not opts["force"] and not prompt_changed:
        return {"status": "skip", "reason": "airs 已经是最新的", "airs": airs,
                "resolved": resolved, "unresolved": unresolved,
                "prompt_fix": prompt_fix}
    extra["airs"] = airs

    if opts["dry_run"]:
        return {"status": "dry-run", "airs": airs, "resolved": resolved,
                "unresolved": unresolved,
                "dropped": [d["name"] for d in dropped],
                "prompt_fix": prompt_fix, "prompt_changed": prompt_changed}

    # 只替换 workflow（必要时加 prompt）两个块，其余块原样
    # tEXt / zTXt 用 latin-1，必须 ensure_ascii=True
    touched = {wf_index}
    report_text = None
    if prompt_changed and "prompt" in texts:
        p_ctype, _p_text, p_comp, p_index = texts["prompt"]
        report_text = json.dumps(prompt_obj, ensure_ascii=(p_ctype != b"iTXt"))
        chunks[p_index][1] = encode_text_chunk(p_ctype, "prompt", report_text, p_comp)
        touched.add(p_index)

    wf_text_new = json.dumps(workflow, ensure_ascii=(wf_ctype != b"iTXt"))
    chunks[wf_index][1] = encode_text_chunk(wf_ctype, "workflow", wf_text_new, wf_comp)
    new_bytes = build_png(chunks)

    # 自检：除 workflow / prompt 外，每个块都必须和原图逐字节相同
    check = parse_png(new_bytes)
    if len(check) != len(chunks):
        raise RuntimeError("块数量发生了变化")
    original_chunks = parse_png(original)
    for i in range(len(chunks)):
        if i not in touched and original_chunks[i][1] != check[i][1]:
            raise RuntimeError("第 {} 个块 {} 被意外改动了".format(
                i, original_chunks[i][0]))
    checked_text = collect_text(check)
    assert json.loads(checked_text["workflow"][1])["extra"]["airs"] == airs
    if report_text is not None:
        assert json.loads(checked_text["prompt"][1]) == prompt_obj

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(new_bytes)

    return {"status": "ok", "airs": airs, "resolved": resolved,
            "unresolved": unresolved,
            "dropped": [d["name"] for d in dropped],
            "prompt_fix": prompt_fix, "prompt_changed": prompt_changed,
            "size_before": len(original), "size_after": len(new_bytes)}


def _find_pngs(input_dir, output_dir):
    files = []
    input_abs = os.path.abspath(input_dir)
    output_abs = os.path.abspath(output_dir)
    for dirpath, _dirnames, filenames in os.walk(input_abs):
        if os.path.abspath(dirpath).startswith(output_abs):
            continue  # 跳过输出目录，避免重复处理自己的产物
        for fn in filenames:
            if fn.lower().endswith(".png"):
                files.append(os.path.join(dirpath, fn))
    return sorted(files)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def inject_airs(input_dir, output_dir=None, suffix="_comfy", model_index=None,
                db=None, hash_cache=None, kinds=DEFAULT_KINDS,
                include_disabled=False, overwrite=False, dry_run=False,
                force=False, allow_api=False, api_base=core.API_BASE_DEFAULT,
                timeout=20, retries=3, fix_prompt=True, fix_values=True,
                cancel=None, progress=None, log=None):
    """批量处理文件夹里的 PNG。

    参数与旧脚本保持一致；model_index / db / hash_cache 由调用方提供。
    返回 dict：total / ok / skipped / errors / results / airs /
    unresolved / prompt_fixed / log。
    """
    if not model_index or db is None or hash_cache is None:
        raise ValueError("model_index / db / hash_cache 必须提供")

    input_dir = os.path.abspath(input_dir)
    if not os.path.isdir(input_dir):
        raise NotADirectoryError("输入目录不存在: {}".format(input_dir))
    output_dir = os.path.abspath(output_dir or os.path.join(input_dir, "add_data"))

    log_lines = []

    def say(message=""):
        log_lines.append(message)
        if log:
            log(message)

    files = _find_pngs(input_dir, output_dir)
    say("输入目录  : {}".format(input_dir))
    say("输出目录  : {}".format(output_dir))
    say("本地模型库: {} 条".format(len(db)))
    say("联网兜底  : {}".format("开启" if allow_api else "关闭（只用本地库）"))
    say("提示词修复: {}".format("开启" if fix_prompt else "关闭")
        + ("（含数值内联）" if fix_values else ""))
    say("待处理    : {} 个 PNG".format(len(files)))
    say()

    opts = {
        "include_disabled": include_disabled,
        "kinds": kinds,
        "dry_run": dry_run,
        "force": force,
        "allow_api": allow_api,
        "api_base": api_base,
        "timeout": timeout,
        "retries": retries,
        "fix_prompt": fix_prompt,
        "fix_values": fix_values,
        "cancel": cancel,
        "log": log,
    }

    # 先把需要的模型并行哈希掉；缓存命中则不读文件
    needed = []
    for path in files:
        core._check_cancel(cancel)
        try:
            with open(path, "rb") as f:
                texts = collect_text(parse_png(f.read()))
            if "workflow" not in texts:
                continue
            wf = json.loads(texts["workflow"][1])
            pr = json.loads(texts["prompt"][1]) if "prompt" in texts else None
            for name, kind in extract_resources(pr, wf, include_disabled):
                local = model_index.find(name, kind)
                if local and hash_cache.get(local) is None:
                    needed.append(local)
        except core.Cancelled:
            raise
        except Exception:
            continue
    needed = list(dict.fromkeys(needed))
    if needed:
        say("需要计算哈希的模型文件 {} 个".format(len(needed)))

        def _hash_one(path):
            return hash_cache.compute(path, cancel=cancel)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(_hash_one, path) for path in needed]
            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                core._check_cancel(cancel)
                try:
                    future.result()
                except core.Cancelled:
                    raise
                except Exception as exc:
                    say("    哈希失败：{}".format(exc))
                core._progress(progress, i, len(needed), "哈希模型")
        hash_cache.save()
        say()

    results, unresolved_all, counter = [], [], {}
    prompt_fixed = 0
    for i, path in enumerate(files, 1):
        core._check_cancel(cancel)
        rel = os.path.relpath(path, input_dir)
        if suffix:
            stem, ext = os.path.splitext(rel)
            rel = stem + suffix + ext
        out_path = os.path.join(output_dir, rel)
        if os.path.exists(out_path) and not overwrite and not dry_run:
            counter["已有输出(跳过)"] = counter.get("已有输出(跳过)", 0) + 1
            core._progress(progress, i, len(files), "已存在 {}".format(os.path.basename(path)))
            continue
        try:
            result = _process_one(path, out_path, model_index, db, hash_cache, opts)
        except core.Cancelled:
            raise
        except Exception as exc:
            result = {"status": "error",
                      "reason": "{}: {}".format(type(exc).__name__, exc)}
        result["file"] = os.path.relpath(path, input_dir)
        results.append(result)
        counter[result["status"]] = counter.get(result["status"], 0) + 1
        if result.get("prompt_changed") and (result.get("prompt_fix") or {}).get("repaired"):
            prompt_fixed += 1
        for unresolved in result.get("unresolved", []):
            unresolved_all.append((result["file"], unresolved[0], unresolved[1]))

        tag = {"ok": "OK  ", "skip": "--  ", "error": "ERR ",
               "dry-run": "DRY "}.get(result["status"], "??? ")
        detail = result.get("reason", "")
        if result["status"] in ("ok", "dry-run"):
            detail = "airs={} {}".format(
                len(result["airs"]), [a.split(":")[-1] for a in result["airs"]])
            pf = result.get("prompt_fix") or {}
            if (pf.get("added_text") or pf.get("created_nodes")) and pf.get("positive"):
                detail += "  提示词✓{}字".format(len(pf["positive"]))
                if pf.get("created_nodes"):
                    detail += "(+负向节点{})".format(",".join(pf["created_nodes"]))
                elif pf.get("negative"):
                    detail += "(含负向)"
        say("  {} {:<44} {}".format(tag, result["file"], detail))
        for name, reason in result.get("unresolved", []):
            say("        ! {} -> {}".format(name, reason))
        core._progress(progress, i, len(files), os.path.basename(path))

    hash_cache.save()
    air_counter = {}
    for result in results:
        for air in result.get("airs", []):
            air_counter[air] = air_counter.get(air, 0) + 1

    summary = {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "total": len(files),
        "ok": counter.get("ok", 0) + counter.get("dry-run", 0),
        "skipped": counter.get("skip", 0) + counter.get("已有输出(跳过)", 0),
        "errors": counter.get("error", 0),
        "results": results,
        "airs": air_counter,
        "unresolved": unresolved_all,
        "prompt_fixed": prompt_fixed,
        "log": log_lines,
    }

    say()
    say("统计: " + ", ".join("{}={}".format(k, v) for k, v in sorted(counter.items())))
    if prompt_fixed:
        say("提示词修复: {} 张图的 Civitai 提示词已补全".format(prompt_fixed))
    if unresolved_all:
        say("有 {} 个模型没能解析，见上面的 ! 行".format(len(unresolved_all)))

    return summary
