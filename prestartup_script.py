# -*- coding: utf-8 -*-
"""
关闭 ComfyUI 0.30 引入的 pinned-memory MRU 权重驻留机制
==========================================================

背景
----
ComfyUI v0.30.0 起，动态显存加载引入新的 pinned-memory 缓存策略
(commit c0117553, "Load weights to process RAM with MRU policy using pinning infrastructure")。
它会额外保留一份"已加载权重"的 pinned 副本驻留在系统内存中，
导致大模型(如 Krea2 12.9GB)在显存和内存中同时占满。

本脚本在 ComfyUI 解析命令行之后、model_management 初始化之前
把 args.disable_pinned_memory 置为 True，等效于启动时加了 --disable-pinned-memory。

为什么这样做
------------
- main.py 在第 4 行就从 comfy.cli_args 拿到已解析的 args(此时 parse_args 已完成)，
  而 prestartup_script.py 在第 225 行才执行 —— 所以往 sys.argv 塞参数已经太晚、无效。
- 但 comfy.model_management 要到 main.py 第 243 行才被 import，
  它读取的是同一个 args 对象(module 级单例)。在这里改属性，model_management 初始化时就能看到。

验证方法
--------
重启 ComfyUI 后，启动日志中 "Enabled pinned memory 19313.0" 这一行应消失。
"""

from comfy.cli_args import args

args.disable_pinned_memory = True
