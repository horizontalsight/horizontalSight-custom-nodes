import json
import os
import re
import time

import numpy as np
from PIL import Image, PngImagePlugin

import folder_paths


def resolve_time_tokens(text):
    return re.sub(r"\[time\((.*?)\)\]", lambda m: time.strftime(m.group(1)), text)


def next_folder_number(folder, delimiter):
    pattern = re.compile(r"(\d+)" + re.escape(delimiter))
    numbers = [int(m.group(1)) for name in os.listdir(folder) if (m := pattern.match(name))]
    return max(numbers, default=0) + 1


class SequenceImageSave:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "output_path": ("STRING", {"default": "[time(%Y-%m-%d)]", "multiline": False}),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "filename_delimiter": ("STRING", {"default": "_"}),
                "filename_number_padding": ("INT", {"default": 6, "min": 1, "max": 9, "step": 1}),
                "extension": (["png", "jpg", "webp"],),
                "quality": ("INT", {"default": 95, "min": 1, "max": 100, "step": 1}),
                "embed_workflow": ("BOOLEAN", {"default": True}),
            },
            "hidden": {
                "prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "image/io"
    DESCRIPTION = "Saves images as number_prefix.ext, continuing one running number across every prefix in the same folder."
    SEARCH_ALIASES = ["save", "sequence", "顺序保存", "流水号"]

    def save_images(self, images, output_path="", filename_prefix="ComfyUI", filename_delimiter="_",
                    filename_number_padding=6, extension="png", quality=95, embed_workflow=True,
                    prompt=None, extra_pnginfo=None):
        folder = resolve_time_tokens(output_path)
        if not os.path.isabs(folder):
            folder = os.path.join(self.output_dir, folder)
        os.makedirs(folder, exist_ok=True)

        prefix = resolve_time_tokens(filename_prefix)
        counter = next_folder_number(folder, filename_delimiter)
        subfolder = os.path.relpath(folder, self.output_dir).replace(os.sep, "/")

        metadata = None
        if extension == "png" and embed_workflow:
            metadata = PngImagePlugin.PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for key in extra_pnginfo:
                    metadata.add_text(key, json.dumps(extra_pnginfo[key]))

        results = list()
        for image in images:
            while True:
                name = f"{counter:0{filename_number_padding}}{filename_delimiter}{prefix}.{extension}"
                if not os.path.exists(os.path.join(folder, name)):
                    break
                counter += 1

            img = Image.fromarray(np.clip(255. * image.cpu().numpy(), 0, 255).astype(np.uint8))
            if extension == "png":
                img.save(os.path.join(folder, name), pnginfo=metadata, compress_level=4)
            else:
                img.save(os.path.join(folder, name), quality=quality)

            results.append({"filename": name, "subfolder": subfolder, "type": self.type})
            counter += 1

        return {"ui": {"images": results}, "result": (images,)}


NODE_CLASS_MAPPINGS = {"SequenceImageSave": SequenceImageSave}

NODE_DISPLAY_NAME_MAPPINGS = {"SequenceImageSave": "顺序保存图像"}
