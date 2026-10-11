# -*- coding: utf-8 -*-
"""horizontalSight 伞包的本地节点子包。

每个文件都保持独立可读：
    image_folder.py   从文件夹加载图片 / 保存图片并把提示词写进工作流元数据
    sequence_save.py  序列化命名保存（时间占位符、序号补零、jpg/webp 质量）
    memory.py         释放模型缓存（含 SeedVR2 的常驻缓存）
"""
