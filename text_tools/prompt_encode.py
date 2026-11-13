# -*- coding: utf-8 -*-
"""CLIP 文本编码（清洗 + 显示）。

和 ComfyUI 自带的 ``CLIPTextEncode`` 等价（同样走 ``clip.tokenize`` +
``clip.encode_from_tokens_scheduled``），只是：

1. 编码前先调用 :func:`text_tools.prompt_clean.clean_prompt` 去掉括号/权重；
2. 把清洗后的文本通过 ``ui.text`` 回传前端，在节点里显示出来。

这样即使 ``text`` 输入被连线（ComfyUI 会隐藏被连线控件的输入框），
节点里仍然能清楚看到“实际送去编码的文本”。
"""

from __future__ import annotations

from .prompt_clean import clean_prompt

__all__ = [
    "PromptCleanEncode",
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
]


class PromptCleanEncode:
    """清洗提示词后直接编码成 CONDITIONING，并在节点内显示清洗结果。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "text": ("STRING", {"multiline": True, "default": ""}),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("conditioning",)
    FUNCTION = "encode"
    CATEGORY = "utils/prompt"
    OUTPUT_NODE = True

    DESCRIPTION = (
        "先去掉提示词里的 (){} 括号和 :1.2 权重，再用接进来的 CLIP 编码；"
        "节点内会显示实际参与编码的清洗后文本。等价于 CLIP文本编码 + 提示词清洗。"
    )
    SEARCH_ALIASES = [
        "clip text encode clean",
        "clean encode",
        "prompt clean encode",
        "提示词清洗编码",
        "清洗文本编码",
        "CLIP文本编码清洗",
    ]

    def encode(self, clip, text):
        if clip is None:
            raise RuntimeError(
                "ERROR: clip input is invalid: None\n\n"
                "If the clip is from a checkpoint loader node your checkpoint "
                "does not contain a valid clip or text encoder model."
            )

        cleaned = clean_prompt(text)
        tokens = clip.tokenize(cleaned)
        conditioning = clip.encode_from_tokens_scheduled(tokens)
        return {"ui": {"text": [cleaned]}, "result": (conditioning,)}


NODE_CLASS_MAPPINGS = {
    "PromptCleanEncode": PromptCleanEncode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptCleanEncode": "CLIP文本编码（清洗+显示）",
}
