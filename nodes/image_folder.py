import json
import os
import re
import time

import numpy as np
import torch
from PIL import Image, ImageOps, PngImagePlugin

import folder_paths

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def resolve_time_tokens(text):
    return re.sub(r"\[time\((.*?)\)\]", lambda m: time.strftime(m.group(1)), text)


def next_folder_number(folder, delimiter):
    pattern = re.compile(r"(\d+)" + re.escape(delimiter))
    numbers = [int(m.group(1)) for name in os.listdir(folder) if (m := pattern.match(name))]
    return max(numbers, default=0) + 1


def list_folder_images(directory):
    return sorted(f for f in os.listdir(directory) if f.lower().endswith(IMAGE_EXTENSIONS))


def embed_prompt_in_workflow(workflow, prompt_text, node_id):
    """Return a copy of the workflow with prompt_text written into one node's text.

    ``node_id`` >= 0 targets that node id; otherwise the first CLIPTextEncode is
    used. A node's first widget value is its text, which is what gets replaced.
    """
    wf = json.loads(json.dumps(workflow, ensure_ascii=False))
    target = None
    for node in wf.get("nodes", []):
        if node_id >= 0:
            if node.get("id") == node_id:
                target = node
                break
        elif node.get("type") == "CLIPTextEncode":
            target = node
            break
    if target is not None:
        values = target.get("widgets_values")
        if isinstance(values, list) and values:
            values[0] = prompt_text
        else:
            target["widgets_values"] = [prompt_text]
    return wf


def split_prompts(text, count):
    """Split the instruct node's joined output back into one prompt per image."""
    parts = [p.strip() for p in re.split(r"={3,}\s*Image\s+\d+\s*={3,}", text)]
    parts = [p for p in parts if p]
    if len(parts) == count:
        return parts
    if count == 1:
        return [text.strip()]
    return parts


class LoadImagesFromFolder:
    """Head node: reads every image in a folder as one batch and passes the folder
    path through, so the save node can re-read the untouched originals."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "directory": ("STRING", {"default": "", "multiline": False,
                                         "tooltip": "Folder holding the images to process."}),
                "max_size": ("INT", {"default": 512, "min": 128, "max": 4096, "step": 64,
                                     "tooltip": "Longest side of each image handed to the model."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING", "INT")
    RETURN_NAMES = ("images", "directory", "count")
    FUNCTION = "load"
    CATEGORY = "image/io"
    DESCRIPTION = "Loads every image in a folder as one batch and passes the folder path through."

    def load(self, directory, max_size):
        if not os.path.isdir(directory):
            raise ValueError("directory does not exist: %s" % directory)
        names = list_folder_images(directory)
        if not names:
            raise ValueError("no images found in %s" % directory)

        frames = []
        for name in names:
            with Image.open(os.path.join(directory, name)) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                w, h = im.size
                scale = min(max_size / max(w, h), 1.0)
                if scale < 1.0:
                    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                   Image.Resampling.LANCZOS)
                frames.append(np.array(im).astype(np.float32) / 255.0)

        height = max(f.shape[0] for f in frames)
        width = max(f.shape[1] for f in frames)
        batch = np.zeros((len(frames), height, width, 3), dtype=np.float32)
        for i, f in enumerate(frames):
            top = (height - f.shape[0]) // 2
            left = (width - f.shape[1]) // 2
            batch[i, top:top + f.shape[0], left:left + f.shape[1]] = f
        return (torch.from_numpy(batch), directory, len(names))


class SaveImagesWithPrompt:
    """Tail node: re-reads the folder's originals and saves each one with the running
    workflow embedded and its inferred prompt written into a workflow node."""

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "directory": ("STRING", {"forceInput": True}),
                "prompts": ("STRING", {"forceInput": True}),
                "output_path": ("STRING", {"default": "", "multiline": False}),
                "filename_prefix": ("STRING", {"default": "ComfyUI"}),
                "filename_delimiter": ("STRING", {"default": "_"}),
                "filename_number_padding": ("INT", {"default": 6, "min": 1, "max": 9, "step": 1}),
                "extension": (["png", "jpg", "webp"],),
                "quality": ("INT", {"default": 95, "min": 1, "max": 100, "step": 1}),
                "embed_workflow": ("BOOLEAN", {"default": True}),
                "prompt_node_id": ("INT", {"default": -1, "min": -1, "max": 0x7FFFFFFF, "step": 1,
                                           "tooltip": "Which workflow node stores the prompt. -1 = first CLIPTextEncode."}),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("saved",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "image/io"
    DESCRIPTION = ("Saves the folder's images with the running workflow embedded and each "
                   "image's prompt stored in a workflow node.")

    def save(self, directory, prompts, output_path, filename_prefix, filename_delimiter,
             filename_number_padding, extension, quality, embed_workflow, prompt_node_id,
             prompt=None, extra_pnginfo=None):
        if not os.path.isdir(directory):
            raise ValueError("directory does not exist: %s" % directory)
        names = list_folder_images(directory)
        texts = split_prompts(prompts, len(names))

        folder = resolve_time_tokens(output_path)
        if not os.path.isabs(folder):
            folder = os.path.join(self.output_dir, folder)
        os.makedirs(folder, exist_ok=True)
        prefix = resolve_time_tokens(filename_prefix)
        counter = next_folder_number(folder, filename_delimiter)

        saved = []
        for index, name in enumerate(names):
            text = texts[index] if index < len(texts) else ""

            metadata = None
            if extension == "png" and embed_workflow:
                metadata = PngImagePlugin.PngInfo()
                if prompt is not None:
                    metadata.add_text("prompt", json.dumps(prompt))
                if extra_pnginfo is not None:
                    for key in extra_pnginfo:
                        value = extra_pnginfo[key]
                        if key == "workflow":
                            value = embed_prompt_in_workflow(value, text, prompt_node_id)
                        metadata.add_text(key, json.dumps(value, ensure_ascii=False))

            while True:
                filename = f"{counter:0{filename_number_padding}}{filename_delimiter}{prefix}.{extension}"
                if not os.path.exists(os.path.join(folder, filename)):
                    break
                counter += 1

            with Image.open(os.path.join(directory, name)) as im:
                img = ImageOps.exif_transpose(im).convert("RGB")
                if extension == "png":
                    img.save(os.path.join(folder, filename), pnginfo=metadata, compress_level=4)
                else:
                    img.save(os.path.join(folder, filename), quality=quality)
            saved.append(filename)
            counter += 1

        return (", ".join(saved),)


NODE_CLASS_MAPPINGS = {
    "LoadImagesFromFolder": LoadImagesFromFolder,
    "SaveImagesWithPrompt": SaveImagesWithPrompt,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LoadImagesFromFolder": "从文件夹读图",
    "SaveImagesWithPrompt": "保存图片(嵌入提示词)",
}
