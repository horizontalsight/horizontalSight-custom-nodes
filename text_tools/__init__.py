# -*- coding: utf-8 -*-
"""text_tools —— 文本处理节点子包。

    prompt_clean.py     PromptClean：提示词清洗（去括号/权重），可单独执行看结果
    prompt_encode.py    PromptCleanEncode：清洗 + CLIP 文本编码，节点内显示清洗结果
    web/                节点内的只读文本预览脚本（由伞包根目录 __init__.py 的
                        WEB_DIRECTORY = "./text_tools/web" 注册给前端）
"""

import logging

from .prompt_clean import PromptClean
from .prompt_encode import PromptCleanEncode

NODE_CLASS_MAPPINGS = {
    "PromptClean": PromptClean,
    "PromptCleanEncode": PromptCleanEncode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptClean": "提示词清洗（去括号/权重）",
    "PromptCleanEncode": "CLIP文本编码（清洗+显示）",
}

logging.info("[horizontalSight] text_tools 已注册节点: %s", list(NODE_CLASS_MAPPINGS))
