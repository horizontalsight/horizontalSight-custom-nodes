# -*- coding: utf-8 -*-
"""civitai_db 的服务层：配置、数据库单写、任务、缓存、模型索引。

不直接依赖 ComfyUI；只有在需要读取 ComfyUI 模型目录时才延迟 import
``folder_paths``，这样核心逻辑和单元测试可以在 ComfyUI 之外运行。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import traceback
from collections import deque

try:
    from . import core, inject
except ImportError:  # 允许在包外单独 import（单元测试）
    import core
    import inject

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(MODULE_DIR, "_local_config_ignore")
DEFAULT_DB_PATH = os.path.join(CONFIG_DIR, "model_db.json")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_HASH_CACHE = os.path.join(CONFIG_DIR, "hash_cache.json")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
LOG_PATH = os.path.join(CONFIG_DIR, "app.log")


def detect_comfyui_root():
    """从插件安装位置推断 ComfyUI 根目录；不扫描本机其它目录。

    插件固定安装在 ``<ComfyUI>/custom_nodes/<包名>/civitai_db``，
    所以从本文件的目录向上找 ``main.py`` + ``folder_paths.py`` 即可。
    只有插件被移动到非标准位置时，才用 folder_paths 兜底。
    """
    current = MODULE_DIR
    for _ in range(8):
        current = os.path.dirname(current)
        if not current or current == os.path.dirname(current):
            break
        if (os.path.isfile(os.path.join(current, "main.py"))
                and os.path.isfile(os.path.join(current, "folder_paths.py"))):
            return current

    try:
        import folder_paths
        base = getattr(folder_paths, "base_path", None)
        if base and os.path.isdir(base):
            return os.path.abspath(base)
    except Exception:
        pass
    return ""


DEFAULT_CONFIG = {
    "db_path": "model_db.json",
    "comfyui_root": detect_comfyui_root(),
    "api_base": core.API_BASE_DEFAULT,
    "request_timeout": 20,
    "query_retries": 3,
    "query_delay": 1.0,
    "max_consecutive_429": 3,
    "scan": {
        "categories": ["diffusion_models", "checkpoints", "loras"],
        "extensions": [".safetensors", ".ckpt", ".pt", ".pth", ".bin"],
        "extra_folders": [],
        "recursive": True,
    },
    "inject": {
        "suffix": "_comfy",
        "kinds": "model,lora",
        "include_disabled": False,
        "overwrite": False,
        "fix_prompt": True,
        "fix_values": True,
        "allow_api": False,
        "output_subdir": "add_data",
    },
    "hash_cache": "hash_cache.json",
    "seed_hash_cache_from": "",
    "backup_keep": 10,
    "allow_remote": False,
    "index_categories": [
        "loras", "checkpoints", "diffusion_models", "vae", "text_encoders",
        "clip", "controlnet", "upscale_models", "unet", "model_patches",
    ],
}


def _deep_merge(base, user):
    for key, value in base.items():
        if key not in user:
            user[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(user.get(key), dict):
            _deep_merge(value, user[key])
        elif isinstance(value, list) and not isinstance(user.get(key), list):
            user[key] = copy.deepcopy(value)
    return user


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self._load()
        self._ensure_dirs()

    def _ensure_dirs(self):
        os.makedirs(os.path.dirname(self.db_path) or CONFIG_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(self.hash_cache_path) or CONFIG_DIR,
                    exist_ok=True)
        os.makedirs(BACKUP_DIR, exist_ok=True)

    def _load(self):
        if not os.path.isfile(self.path):
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                user = json.load(f)
            if isinstance(user, dict):
                _deep_merge(DEFAULT_CONFIG, user)
                self.data = user
        except Exception:
            self.data = copy.deepcopy(DEFAULT_CONFIG)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or CONFIG_DIR, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def db_path(self):
        value = self.data.get("db_path") or "model_db.json"
        return value if os.path.isabs(value) else os.path.join(CONFIG_DIR, value)

    @property
    def hash_cache_path(self):
        value = self.data.get("hash_cache") or "hash_cache.json"
        return value if os.path.isabs(value) else os.path.join(CONFIG_DIR, value)

    @property
    def comfyui_root(self):
        value = (self.data.get("comfyui_root") or "").strip()
        if not value:
            value = detect_comfyui_root()
        return os.path.abspath(os.path.normpath(value)) if value else ""

    @property
    def models_root(self):
        root = self.comfyui_root
        return os.path.join(root, "models") if root else ""

    @property
    def backup_keep(self):
        try:
            return int(self.data.get("backup_keep", 10))
        except (TypeError, ValueError):
            return 10

    def update(self, values):
        if not isinstance(values, dict):
            return
        _deep_merge(DEFAULT_CONFIG, values)
        self.data = values
        self._ensure_dirs()
        self.save()


class DatabaseService:
    """单写锁 + mtime 重载 + 备份；损坏时拒绝覆盖。"""

    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.db = {}
        self.mtime = None
        self.corrupt = False
        self.error = None
        self.reload(force=True)

    @property
    def path(self):
        return self.config.db_path

    def reload(self, force=False):
        with self.lock:
            path = self.path
            if not os.path.isfile(path):
                self.db = {}
                self.mtime = None
                self.corrupt = False
                self.error = None
                return
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            if not force and mtime == self.mtime and self.db:
                return
            try:
                self.db = core.load_db(path, strict=True)
                self.mtime = mtime
                self.corrupt = False
                self.error = None
            except Exception as exc:
                self.corrupt = True
                self.error = str(exc)
                # 保留旧内存数据；没有就空库。绝不把空库写回磁盘。
                if not isinstance(self.db, dict):
                    self.db = {}

    def save(self, backup=False):
        with self.lock:
            if self.corrupt:
                raise core.DBError("数据库损坏，已拒绝覆盖：{}".format(self.error))
            core.save_db(self.db, self.path,
                         backup_dir=BACKUP_DIR if backup else None,
                         keep_backups=self.config.backup_keep)
            try:
                self.mtime = os.path.getmtime(self.path)
            except OSError:
                self.mtime = None

    def stats(self):
        with self.lock:
            return core.db_stats(self.db)

    def entries(self):
        with self.lock:
            rows = []
            for av, entry in self.db.items():
                if not isinstance(entry, dict):
                    continue
                files = list(entry.get("file") or [])
                paths = list(entry.get("paths") or [])
                display = (files[0] if files else
                           (os.path.basename(paths[0]) if paths else
                            (entry.get("modelName") or av)))
                rows.append({
                    "key": av,
                    "file": display,
                    "files": files,
                    "paths": paths,
                    "modelName": entry.get("modelName") or "",
                    "AIR": entry.get("AIR") or "",
                    "type": entry.get("type") or "",
                    "baseModel": entry.get("baseModel") or "",
                    "status": core.entry_status(av, entry),
                    "failed": bool(entry.get("failed")),
                    "failedKind": entry.get("failedKind"),
                    "failedCount": int(entry.get("failedCount") or 0),
                    "ignored": bool(entry.get("ignored")),
                    "url": entry.get("url") or core.model_url(entry),
                    "queriedAt": entry.get("queriedAt"),
                    "lastTriedAt": entry.get("lastTriedAt"),
                    "error": entry.get("error") or "",
                    "pathExists": any(os.path.exists(p) for p in paths if p),
                })
            rows.sort(key=lambda r: ((r["baseModel"] or "").lower(),
                                     (r["type"] or "").lower(),
                                     (r["file"] or "").lower()))
            return rows

    def get_entry(self, av):
        with self.lock:
            return copy.deepcopy(self.db.get(av))

    def put_entry(self, av, entry):
        with self.lock:
            if self.corrupt:
                raise core.DBError("数据库损坏，已拒绝写入：{}".format(self.error))
            ok, errors = core.validate_db_entry(entry)
            if not ok:
                raise ValueError("；".join(errors))
            core.normalize_db({av: entry})
            self.db[av] = entry
            self.save(backup=True)
            return entry

    def delete_entry(self, av):
        with self.lock:
            if self.corrupt:
                raise core.DBError("数据库损坏，已拒绝写入：{}".format(self.error))
            if av not in self.db:
                return False
            del self.db[av]
            self.save(backup=True)
            return True

    def health(self):
        with self.lock:
            return core.check_db_integrity(self.db)


class Job:
    def __init__(self, job_id, action, params):
        self.id = job_id
        self.action = action
        self.params = params
        self.state = "queued"
        self.total = 0
        self.current = 0
        self.detail = ""
        self.message = ""
        self.result = None
        self.error = None
        self.cancel_event = threading.Event()
        self.logs = deque(maxlen=1000)
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.finished_at = None

    def log(self, message):
        line = "[{}] {}".format(time.strftime("%H:%M:%S"), message)
        self.logs.append(line)

    def progress(self, done, total, detail=""):
        self.current = int(done)
        self.total = int(total)
        if detail:
            self.detail = str(detail)

    def as_dict(self):
        return {
            "id": self.id,
            "action": self.action,
            "state": self.state,
            "total": self.total,
            "current": self.current,
            "detail": self.detail,
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": list(self.logs)[-300:],
        }


class JobManager:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.current = None
        self._counter = 0

    def is_running(self):
        with self.lock:
            return bool(self.current and self.current.state in ("queued", "running"))

    def start(self, action, params=None):
        with self.lock:
            if self.is_running():
                raise RuntimeError("已有维护任务在运行，请先等待或取消。")
            self._counter += 1
            job = Job("job-{}-{}".format(int(time.time()), self._counter),
                      action, params or {})
            self.current = job
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        thread.start()
        return job.id

    def _run(self, job):
        job.state = "running"
        try:
            result = self.service.execute(job.action, job.params, job.log,
                                          job.progress, job.cancel_event)
            job.result = result
            job.state = "cancelled" if job.cancel_event.is_set() else "done"
            job.message = "完成"
        except core.Cancelled:
            job.state = "cancelled"
            job.message = "已取消"
        except Exception as exc:
            job.state = "error"
            job.message = "{}: {}".format(type(exc).__name__, exc)
            job.error = traceback.format_exc()
            job.log("错误：{}".format(job.message))
        finally:
            job.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                self.service.database.reload()
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return self.current.as_dict() if self.current else None

    def cancel(self):
        with self.lock:
            if self.current and self.current.state in ("queued", "running"):
                self.current.cancel_event.set()
                self.current.log("收到取消请求")
                return True
        return False


class CivitaiService:
    def __init__(self, config=None):
        self.config = config or Config()
        self.database = DatabaseService(self.config)
        self.jobs = JobManager(self)
        self._hash_cache = None
        self._index = None
        self._index_at = 0
        self._lock = threading.RLock()
        self._log_to_file("启动 civitai_db 服务")

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def _log_to_file(self, message):
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("[{}] {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"),
                                           message))
        except Exception:
            pass
        logging.info("[civitai_db] %s", message)

    def log(self, message):
        self._log_to_file(message)

    def hash_cache(self):
        with self._lock:
            if self._hash_cache is None:
                seed = self.config.get("seed_hash_cache_from") or ""
                self._hash_cache = core.HashCache(self.config.hash_cache_path,
                                                  seed_path=seed)
                if seed and self._hash_cache.data:
                    try:
                        self._hash_cache.save()
                    except Exception:
                        pass
            return self._hash_cache

    def folder_roots(self, categories=None):
        """返回 [(category, path), ...]；ComfyUI 不存在时返回空。"""
        try:
            import folder_paths
        except Exception:
            return []
        roots = []
        seen = set()
        wanted = categories or self.config.get("index_categories") or []
        if isinstance(wanted, str):
            wanted = [wanted]
        for category in wanted:
            try:
                paths = folder_paths.get_folder_paths(category)
            except Exception:
                continue
            for path in paths or []:
                key = os.path.normcase(os.path.normpath(path))
                if key in seen or not os.path.isdir(path):
                    continue
                seen.add(key)
                roots.append((category, path))
        return roots

    def model_index(self, force=False):
        with self._lock:
            now = time.time()
            if not force and self._index is not None and now - self._index_at < 120:
                return self._index
            index = core.ModelIndex()
            for category, path in self.folder_roots():
                index.add_root(path, category)
            for path in self.config.get("scan", {}).get("extra_folders") or []:
                index.add_root(path, "extra")
            self._index = index
            self._index_at = now
            self.log("模型索引：{} 个文件".format(len(index.files)))
            return index

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def execute(self, action, params=None, log=None, progress=None, cancel=None):
        params = params or {}
        log = log or self.log
        self.database.reload()
        if self.database.corrupt:
            raise core.DBError("数据库损坏：{}".format(self.database.error))

        if action == "scan":
            return self._scan(params, log, progress, cancel)
        if action == "verify":
            return self._verify(params, log, progress, cancel)
        if action == "hash_inc":
            return self._hash(params, log, progress, cancel, full=False)
        if action == "hash_full":
            return self._hash(params, log, progress, cancel, full=True)
        if action in ("query_new", "query_not_found", "query_failed",
                      "query_selected"):
            mode = {"query_new": "new", "query_not_found": "not_found",
                    "query_failed": "failed",
                    "query_selected": "selected"}[action]
            return self._query(mode, params, log, progress, cancel)
        if action == "inject":
            return self._inject(params, log, progress, cancel)
        raise ValueError("未知动作：{}".format(action))

    def _scan(self, params, log, progress, cancel):
        folder = (params.get("folder") or "").strip()
        categories = params.get("categories")
        extensions = core.parse_extensions(
            params.get("extensions") or self.config.get("scan", {}).get("extensions"))
        recursive = params.get("recursive", True)
        if folder:
            roots = [folder]
        else:
            cats = categories or self.config.get("scan", {}).get("categories")
            roots = [path for _cat, path in self.folder_roots(cats)]
            roots.extend(self.config.get("scan", {}).get("extra_folders") or [])
        if not roots:
            raise RuntimeError("没有可扫描的模型目录（ComfyUI 未注册或路径为空）。")
        log("[扫描] 根目录：{}".format("; ".join(roots)))
        self.database.save(backup=True)
        summary = core.scan_roots(
            roots, self.database.db, extensions=extensions,
            recursive=bool(recursive), cancel=cancel, progress=progress,
            log=log, save_cb=lambda db: self.database.save(backup=False))
        return summary

    def _verify(self, params, log, progress, cancel):
        self.database.save(backup=True)
        return core.verify_local_paths(
            self.database.db, cancel=cancel, progress=progress, log=log,
            save_cb=lambda db: self.database.save(backup=False))

    def _hash(self, params, log, progress, cancel, full):
        self.database.save(backup=True)
        return core.compute_hashes(
            self.database.db, self.hash_cache(), full=full,
            force=bool(params.get("force")), cancel=cancel, progress=progress,
            log=log, save_cb=lambda db: self.database.save(backup=False))

    def _query(self, mode, params, log, progress, cancel):
        self.database.save(backup=True)
        return core.query_records(
            self.database.db, mode=mode,
            api_base=self.config.get("api_base"),
            timeout=int(self.config.get("request_timeout") or 20),
            retries=int(self.config.get("query_retries") or 3),
            delay=float(self.config.get("query_delay") or 1.0),
            limit=int(params.get("limit") or 0),
            max_consecutive_429=int(self.config.get("max_consecutive_429") or 3),
            selected=params.get("selected"),
            cancel=cancel, progress=progress, log=log,
            save_cb=lambda db: self.database.save(backup=False))

    def _inject(self, params, log, progress, cancel):
        input_dir = (params.get("input") or "").strip()
        if not input_dir or not os.path.isdir(input_dir):
            raise RuntimeError("输入目录不存在：{}".format(input_dir))
        output_dir = (params.get("output") or "").strip() or None
        if not output_dir:
            subdir = self.config.get("inject", {}).get("output_subdir") or "add_data"
            output_dir = os.path.join(input_dir, subdir)
        defaults = self.config.get("inject") or {}
        with self.database.lock:
            db = copy.deepcopy(self.database.db)
        dry_run = bool(params.get("dry_run", True))
        return inject.inject_airs(
            input_dir, output_dir=output_dir,
            suffix=params.get("suffix", defaults.get("suffix", "_comfy")),
            model_index=self.model_index(),
            db=db, hash_cache=self.hash_cache(),
            kinds=params.get("kinds", defaults.get("kinds", "model,lora")),
            include_disabled=bool(params.get("include_disabled",
                                            defaults.get("include_disabled", False))),
            overwrite=bool(params.get("overwrite", defaults.get("overwrite", False))),
            dry_run=dry_run,
            force=bool(params.get("force", False)),
            allow_api=bool(params.get("allow_api", defaults.get("allow_api", False))),
            api_base=self.config.get("api_base"),
            timeout=int(self.config.get("request_timeout") or 20),
            retries=int(self.config.get("query_retries") or 3),
            fix_prompt=bool(params.get("fix_prompt", defaults.get("fix_prompt", True))),
            fix_values=bool(params.get("fix_values", defaults.get("fix_values", True))),
            cancel=cancel, progress=progress, log=log,
        )

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------
    def lookup(self, model, auto_hash=False, allow_network=False):
        db = self.database.db
        av, entry = core.lookup_by_filename(db, model)
        path = None
        if entry is None and model and os.path.isfile(model):
            path = os.path.abspath(model)
            try:
                digest = self.hash_cache().compute(path)
                av = core.autov2(digest)
                entry = db.get(av)
            except Exception as exc:
                return {"status": "error", "message": str(exc), "air": ""}
        if entry is None:
            if auto_hash and not path:
                found = self.model_index().find(model)
                if found:
                    path = found
                    digest = self.hash_cache().compute(path)
                    av = core.autov2(digest)
                    entry = db.get(av)
        if entry is None and allow_network and av:
            api_entry, error = core.query_by_autov2(
                av, api_base=self.config.get("api_base"),
                timeout=int(self.config.get("request_timeout") or 20),
                retries=int(self.config.get("query_retries") or 3))
            if api_entry:
                entry = api_entry
            elif error:
                return {"status": error.get("kind") or "error",
                        "message": error.get("message") or "", "air": "",
                        "auto_v2": av, "path": path}
        if entry is None:
            return {"status": "not_found", "message": "数据库里没有这个模型",
                    "air": "", "auto_v2": av, "path": path}
        status = core.entry_status(av, entry)
        return {
            "status": status,
            "air": entry.get("AIR") or "",
            "model_name": entry.get("modelName") or "",
            "auto_v2": av,
            "path": path,
            "entry": entry,
            "message": "" if status == "ok" else (entry.get("error") or ""),
        }

    def snapshot(self):
        stats = self.database.stats()
        job = self.jobs.snapshot()
        return {
            "db_path": self.config.db_path,
            "comfyui_root": self.config.comfyui_root,
            "models_root": self.config.models_root,
            "corrupt": self.database.corrupt,
            "db_error": self.database.error,
            "stats": stats,
            "job": job,
            "config": self.config.data,
        }
