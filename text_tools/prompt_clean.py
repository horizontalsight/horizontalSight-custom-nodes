# -*- coding: utf-8 -*-
"""SD WebUI 提示词清洗：去掉括号与冒号权重，输出纯文本。

规则（与需求一致）：
1. 删除 ``(`` ``)`` ``{`` ``}`` 四种括号字符（含被反斜杠转义的写法）；
2. 删除 ``:1.2`` / ``: 0.9`` / ``:2`` 这类权重后缀，其余字符原样保留
   （逗号、空格都不做压缩，所以 ``, ((a))`` 清洗后是 ``, a``）。

权重只在与 SD 语法吻合的位置才删除：
- 冒号前一位不能是数字，避免误伤 ``16:9``、``8:00``、``1080:1920``；
- 数字后必须是逗号 / 右括号 / 行尾等“权重结束”位置，避免误伤 ``color:red``。

ComfyUI 节点 ``PromptClean``：
    text(STRING，可连接输入) + clip(CLIP，可选)
        ->  text(STRING) + clip(CLIP 直通) + conditioning(CONDITIONING)
"""

from __future__ import annotations

import re

__all__ = [
    "PromptClean",
    "clean_prompt",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]

# 冒号权重：`(text:1.2)` / `(text : 1.2)` / 裸露的 `text:1.2,`
# - (?<![0-9])  冒号左边不能是数字：保护 16:9、8:00 这类比例 / 时间
# - [ \t]*      允许冒号与数字之间有空格
# - 数字后必须紧跟 , ) ] } 换行 或 文本结尾（允许中间有空格、制表符）
_WEIGHT_RE = re.compile(
    r"(?<![0-9]):[ \t]*(?:\d+(?:\.\d+)?|\.\d+)(?=[ \t]*(?:[,)\]}\r\n]|$))",
    re.MULTILINE,
)

# 转义括号（SD 里 \( 表示字面括号）连同反斜杠一起删掉，避免留下孤立的 “\”
_ESCAPED_BRACKET_RE = re.compile(r"\\[(){}]")

# 其余括号字符
_BRACKET_RE = re.compile(r"[(){}]")


def clean_prompt(text: str) -> str:
    """把 SD WebUI 的括号/权重提示词转成纯文本。

    Args:
        text: 原始提示词，允许为 None（返回空串）。

    Returns:
        清洗后的提示词。除删除括号与权重外，逗号、空格、换行都保持原样；
        只去掉整段文本首尾的空白。
    """
    if not text:
        return ""

    cleaned = _ESCAPED_BRACKET_RE.sub("", text)
    cleaned = _WEIGHT_RE.sub("", cleaned)
    cleaned = _BRACKET_RE.sub("", cleaned)
    return cleaned.strip()


class PromptClean:
    """去括号 / 去权重；可选直接编码出 CONDITIONING，方便一个节点走完。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                # 可选直通：接上后本节点就串在 CLIP 与 CLIP文本编码 之间；
                # 同时用来计算 conditioning 输出。
                "clip": ("CLIP",),
            },
        }

    RETURN_TYPES = ("STRING", "CLIP", "CONDITIONING")
    RETURN_NAMES = ("text", "clip", "conditioning")
    FUNCTION = "clean"
    CATEGORY = "utils/prompt"
    OUTPUT_NODE = True

    DESCRIPTION = (
        "去掉 SD WebUI 提示词里的 (){} 括号和 :1.2 权重，逗号/空格原样保留。"
        "输出清洗后的 text、原样直通的 clip；接上 clip 时还会输出 conditioning"
        "（等价于 CLIP文本编码，可直接接 KSampler）。"
    )
    SEARCH_ALIASES = [
        "prompt clean",
        "clean prompt",
        "remove weights",
        "strip brackets",
        "deweight",
        "提示词清洗",
        "去括号",
        "去权重",
        "清理提示词",
    ]

    def clean(self, text, clip=None):
        cleaned = clean_prompt(text)

        # clip 接上时顺便编码。这里不做“输出没被用到就跳过”的优化：
        # 输出必须只由输入决定，否则会命中 ComfyUI 的缓存、拿到旧的 None。
        conditioning = None
        if clip is not None:
            tokens = clip.tokenize(cleaned)
            conditioning = clip.encode_from_tokens_scheduled(tokens)

        # ui.text：给前端在节点右侧的只读框显示清洗结果。
        # result  ：text / clip / conditioning 三个输出。
        return {"ui": {"text": [cleaned]}, "result": (cleaned, clip, conditioning)}


NODE_CLASS_MAPPINGS = {
    "PromptClean": PromptClean,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptClean": "提示词清洗（去括号/权重）",
}
