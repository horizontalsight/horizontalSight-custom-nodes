# -*- coding: utf-8 -*-
"""Civitai 数据库核心逻辑（无界面、纯标准库）。

从 ``civitai_batch_metadata/civitai_core.py`` 精简而来，只保留卡片 2 需要的能力：
哈希、数据库、扫描登记、校验路径、增量/全量哈希、按状态联网查询、体检。

设计约束：
* 不导入 tkinter / Pillow / ComfyUI；
* 主键仍是 AutoV2（SHA256 前 10 位大写），旧库可直接读写；
* 新增字段只追加，不改变旧字段含义；
* 所有长任务都接受 ``cancel``（threading.Event 或可调用）和 ``progress`` 回调。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MODEL_EXTENSIONS_DEFAULT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")
BROAD_MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin",
                          ".gguf", ".sft")

API_BASE_DEFAULT = "https://civitai.red"

DB_FIELD_DEFAULTS = {
    "file": [],
    "paths": [],
    "modelName": "",
    "AIR": None,
    "modelId": None,
    "modelVersionId": None,
    "url": None,
    "type": None,
    "baseModel": None,
    "baseModelType": None,
    "queriedAt": None,
    "failed": False,
    "error": None,
    # 新增字段（旧库没有也兼容）
    "failedKind": None,     # not_found | error | no_air | None
    "failedCount": 0,
    "lastTriedAt": None,
    "ignored": False,
}

FAILED_KINDS = ("not_found", "error", "no_air")


class DBError(RuntimeError):
    """数据库文件损坏或格式不正确。"""


class Cancelled(RuntimeError):
    """任务被取消。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def norm_path(path):
    if not path:
        return path
    return os.path.normpath(str(path))


def norm_key(path):
    return norm_path(path).lower()


def parse_extensions(values):
    """把 ["safetensors", ".CKPT"] / "a,b" 规范成小写带点元组。"""
    if values is None:
        return MODEL_EXTENSIONS_DEFAULT
    if isinstance(values, str):
        values = [x for x in values.replace(";", ",").split(",")]
    out = []
    for value in values:
        value = str(value).strip().lower()
        if not value:
            continue
        if not value.startswith("."):
            value = "." + value
        if value not in out:
            out.append(value)
    return tuple(out)


def is_pending_key(key):
    return isinstance(key, str) and key.startswith("pending:")


def pending_key_for_path(path):
    return "pending:" + norm_path(path)


def _cancelled(cancel):
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    return bool(cancel.is_set())


def _check_cancel(cancel):
    if _cancelled(cancel):
        raise Cancelled("任务已取消")


def _log(log, message):
    if log:
        log(message)


def _progress(progress, done, total, detail=""):
    if not progress:
        return
    try:
        progress(done, total, detail)
    except TypeError:
        progress(done, total)


def _sleep(seconds, cancel=None):
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        _check_cancel(cancel)
        time.sleep(min(0.2, max(0.0, end - time.time())))


# ---------------------------------------------------------------------------
# 哈希
# ---------------------------------------------------------------------------
def sha256_hex(filepath, cancel=None, chunk_size=8 << 20):
    """分块计算 SHA256；每块检查取消，14GB 文件也能及时停。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            _check_cancel(cancel)
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def autov2(sha256):
    return sha256[:10].upper()


class HashCache:
    """(绝对路径, 大小, mtime 秒) -> SHA256 的本地缓存。

    键格式与 civitai_ckpt_inject/.cache/sha256.json 完全一致，便于首次导入。
    """

    def __init__(self, path, seed_path=None):
        self.path = path
        self._lock = threading.Lock()
        self.data = {}
        self._load()
        if seed_path:
            self.seed_from(seed_path)

    def _load(self):
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.data = {k: v for k, v in data.items()
                             if isinstance(k, str) and isinstance(v, str)}
        except Exception:
            self.data = {}

    @staticmethod
    def key(path):
        st = os.stat(path)
        return "{}|{}|{}".format(os.path.abspath(path), st.st_size,
                                 int(st.st_mtime))

    def get(self, path):
        try:
            key = self.key(path)
        except OSError:
            return None
        with self._lock:
            return self.data.get(key)

    def put(self, path, digest):
        try:
            key = self.key(path)
        except OSError:
            return
        with self._lock:
            self.data[key] = digest

    def compute(self, path, cancel=None, force=False):
        if not force:
            cached = self.get(path)
            if cached:
                return cached
        digest = sha256_hex(path, cancel=cancel)
        self.put(path, digest)
        return digest

    def seed_from(self, other_path):
        if not other_path or not os.path.isfile(other_path):
            return 0
        try:
            with open(other_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        if not isinstance(data, dict):
            return 0
        added = 0
        with self._lock:
            for key, value in data.items():
                if (isinstance(key, str) and isinstance(value, str)
                        and len(value) == 64 and key not in self.data):
                    self.data[key] = value
                    added += 1
        return added

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with self._lock:
            payload = dict(self.data)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def prune(self, keep_paths=None):
        """删除不在 keep_paths 里的缓存键；keep_paths 为路径集合或 None。"""
        if keep_paths is None:
            return 0
        keep = {os.path.abspath(p) for p in keep_paths}
        removed = 0
        with self._lock:
            for key in list(self.data):
                path = key.split("|", 1)[0]
                if path not in keep:
                    del self.data[key]
                    removed += 1
        return removed


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def _unique_file_name(db, target_av, base_name):
    stem, ext = os.path.splitext(base_name)
    candidate = base_name
    n = 2
    while True:
        duplicated = False
        for av, entry in db.items():
            if av == target_av or not isinstance(entry, dict):
                continue
            if candidate in (entry.get("file") or []):
                duplicated = True
                break
        if not duplicated:
            return candidate
        candidate = "{} ({}){}".format(stem, n, ext)
        n += 1


def _dedupe_strings(values):
    seen = set()
    out = []
    for value in values:
        if not isinstance(value, str):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def normalize_db(db):
    """补齐字段、迁移 404 状态、清理重复文件名/路径。返回是否有改动。"""
    if not isinstance(db, dict):
        return False
    changed = False
    for av, entry in list(db.items()):
        if not isinstance(entry, dict):
            continue

        for key, default in DB_FIELD_DEFAULTS.items():
            if key not in entry:
                entry[key] = list(default) if isinstance(default, list) else default
                changed = True

        if not isinstance(entry.get("file"), list):
            entry["file"] = []
            changed = True
        if not isinstance(entry.get("paths"), list):
            entry["paths"] = []
            changed = True
        if not isinstance(entry.get("failed"), bool):
            entry["failed"] = bool(entry.get("failed"))
            changed = True
        if not isinstance(entry.get("ignored"), bool):
            entry["ignored"] = bool(entry.get("ignored"))
            changed = True
        if not isinstance(entry.get("failedCount"), int) or entry["failedCount"] < 0:
            entry["failedCount"] = int(entry.get("failedCount") or 0)
            changed = True

        files = _dedupe_strings(entry.get("file") or [])
        if files != entry["file"]:
            entry["file"] = files
            changed = True
        paths = _dedupe_strings(entry.get("paths") or [])
        if paths != entry["paths"]:
            entry["paths"] = paths
            changed = True

        kind = entry.get("failedKind")
        if kind not in (None,) + FAILED_KINDS:
            entry["failedKind"] = "error" if entry.get("failed") else None
            changed = True

        if entry.get("failed"):
            if not entry.get("failedKind"):
                error = entry.get("error") or ""
                entry["failedKind"] = "not_found" if "404" in error else "error"
                changed = True
            if not entry.get("failedCount"):
                entry["failedCount"] = 1
                changed = True
            if not entry.get("lastTriedAt"):
                entry["lastTriedAt"] = entry.get("queriedAt") or now_str()
                changed = True
        elif entry.get("AIR"):
            if entry.get("failedKind") is not None:
                entry["failedKind"] = None
                changed = True
        else:
            # 旧库：没有 AIR、有查询时间、但 failed=False -> no_air
            if entry.get("failedKind") is None and entry.get("queriedAt"):
                entry["failedKind"] = "no_air"
                changed = True

        # 旧记录没有 file 时，从 paths 回填
        if not entry["file"] and entry["paths"]:
            for path in entry["paths"]:
                name = os.path.basename(str(path).rstrip("\\/"))
                if not name:
                    continue
                unique = _unique_file_name(db, av, name)
                if unique not in entry["file"]:
                    entry["file"].append(unique)
                    changed = True
    return changed


def validate_db_entry(entry):
    """校验单条记录，返回 (ok, errors)。"""
    errors = []
    if not isinstance(entry, dict):
        return False, ["条目必须是 JSON 对象"]

    for key in ("file", "paths"):
        if not isinstance(entry.get(key, []), list):
            errors.append("{} 必须是数组".format(key))
        else:
            for i, value in enumerate(entry.get(key, [])):
                if not isinstance(value, str):
                    errors.append("{}[{}] 必须是字符串".format(key, i))

    for key in ("modelName", "AIR", "type", "baseModel", "baseModelType",
                "error", "url", "queriedAt", "lastTriedAt"):
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            errors.append("{} 必须是字符串或 null".format(key))

    for key in ("modelId", "modelVersionId"):
        value = entry.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append("{} 必须是整数或 null".format(key))

    if "failed" in entry and not isinstance(entry["failed"], bool):
        errors.append("failed 必须是布尔值")
    if "ignored" in entry and not isinstance(entry["ignored"], bool):
        errors.append("ignored 必须是布尔值")
    if "failedCount" in entry and (not isinstance(entry["failedCount"], int)
                                   or isinstance(entry["failedCount"], bool)
                                   or entry["failedCount"] < 0):
        errors.append("failedCount 必须是非负整数")
    if entry.get("failedKind") not in (None,) + FAILED_KINDS:
        errors.append("failedKind 必须是 null / not_found / error / no_air")

    return (len(errors) == 0), errors


def load_db(db_path, strict=True):
    """读取 model_db.json。

    strict=True 时，文件损坏抛 DBError，绝不返回空库去覆盖原文件。
    """
    if not db_path or not os.path.isfile(db_path):
        return {}
    try:
        with open(db_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        if strict:
            raise DBError("读取 model_db.json 失败：{}".format(exc)) from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise DBError("model_db.json 顶层必须是 JSON 对象")
        return {}
    normalize_db(data)
    return data


def _backup_path(backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(backup_dir, "model_db-{}.json".format(stamp))
    n = 2
    while os.path.exists(path):
        path = os.path.join(backup_dir, "model_db-{}-{}.json".format(stamp, n))
        n += 1
    return path


def _prune_backups(backup_dir, keep):
    if not backup_dir or keep <= 0:
        return
    try:
        files = [os.path.join(backup_dir, n) for n in os.listdir(backup_dir)
                 if n.startswith("model_db-") and n.endswith(".json")]
    except OSError:
        return
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for path in files[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


def save_db(db, db_path, backup_dir=None, keep_backups=10):
    """原子保存；可选先备份到 backup_dir，并只保留最近 keep_backups 份。"""
    normalize_db(db)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    if backup_dir and os.path.isfile(db_path):
        shutil.copy2(db_path, _backup_path(backup_dir))
        _prune_backups(backup_dir, keep_backups)
    tmp = db_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, db_path)


def entry_status(av, entry):
    if is_pending_key(av):
        return "pending"
    if not isinstance(entry, dict):
        return "invalid"
    if entry.get("ignored"):
        return "ignored"
    if entry.get("AIR") and not entry.get("failed"):
        return "ok"
    kind = entry.get("failedKind")
    if kind == "not_found":
        return "not_found"
    if kind == "no_air":
        return "no_air"
    if entry.get("failed") or kind == "error":
        return "error"
    if not entry.get("queriedAt"):
        return "new"
    return "no_air"


def db_stats(db):
    stats = {"total": 0, "ok": 0, "pending": 0, "new": 0, "not_found": 0,
             "error": 0, "no_air": 0, "ignored": 0, "no_paths": 0,
             "to_query_new": 0, "to_query_not_found": 0, "to_query_failed": 0}
    for av, entry in (db or {}).items():
        stats["total"] += 1
        status = entry_status(av, entry)
        if status in stats:
            stats[status] += 1
        if isinstance(entry, dict) and not entry.get("paths"):
            stats["no_paths"] += 1
    stats["to_query_new"] = stats["new"]
    stats["to_query_not_found"] = stats["not_found"]
    stats["to_query_failed"] = stats["error"] + stats["no_air"]
    return stats


def check_db_integrity(db):
    """体检；外部路径（旧 webui 链接目录）不算问题。"""
    issues = []
    if not isinstance(db, dict):
        return [{"severity": "error", "code": "db_type", "key": "-",
                 "message": "数据库不是 JSON 对象", "suggestion": "请恢复备份"}]

    name_map = {}
    for av, entry in db.items():
        if not isinstance(entry, dict):
            issues.append({"severity": "error", "code": "entry_type", "key": av,
                           "message": "记录不是 JSON 对象", "suggestion": "删除或修复"})
            continue
        name = entry.get("modelName") or av
        paths = entry.get("paths") or []
        files = entry.get("file") or []
        if not paths:
            issues.append({"severity": "warn", "code": "no_paths", "key": av,
                           "message": "{} 没有本地路径".format(name),
                           "suggestion": "扫描模型目录补录，或删除该旧记录"})
        if not files:
            issues.append({"severity": "warn", "code": "no_file", "key": av,
                           "message": "{} 没有文件名".format(name),
                           "suggestion": "扫描或编辑补录"})
        for path in paths:
            if path and not os.path.exists(path):
                issues.append({"severity": "error", "code": "path_missing", "key": av,
                               "message": "路径不存在：{}".format(path),
                               "suggestion": "文件可能被移动；扫描新目录可补录"})
            if files and path and os.path.basename(path) not in files:
                issues.append({"severity": "warn", "code": "file_mismatch", "key": av,
                               "message": "file 与路径文件名不一致：{}".format(path),
                               "suggestion": "编辑该记录"})
        for f in files:
            if f and not os.path.splitext(f)[1]:
                issues.append({"severity": "info", "code": "no_extension", "key": av,
                               "message": "文件名没有扩展名：{}".format(f),
                               "suggestion": "确认是否同名模型的旧记录"})
            if f:
                name_map.setdefault(os.path.basename(f).lower(), []).append(av)

    for name, avs in name_map.items():
        if len(avs) > 1:
            issues.append({"severity": "warn", "code": "duplicate_name", "key": ",".join(avs),
                           "message": "多个记录使用相同文件名：{}".format(name),
                           "suggestion": "确认是否为同一模型，必要时删除多余记录"})
    return issues


def model_url(entry):
    if not entry:
        return ""
    mid = entry.get("modelId")
    if not mid:
        return ""
    vid = entry.get("modelVersionId")
    if vid:
        return "https://civitai.red/models/{}?modelVersionId={}".format(mid, vid)
    return "https://civitai.red/models/{}".format(mid)


def db_filename_map(db):
    index = {}
    for av, entry in (db or {}).items():
        if not isinstance(entry, dict):
            continue
        for value in list(entry.get("file") or []) + list(entry.get("paths") or []):
            if not value:
                continue
            base = os.path.basename(str(value).replace("/", "\\")).lower()
            if base:
                index.setdefault(base, []).append(av)
    for key, values in index.items():
        index[key] = list(dict.fromkeys(values))
    return index


def lookup_by_filename(db, name):
    """按文件名/路径查记录；返回 (av, entry) 或 (None, None)（含歧义）。"""
    if not name:
        return None, None
    base = os.path.basename(str(name).replace("/", "\\")).lower()
    avs = db_filename_map(db).get(base, [])
    if len(avs) == 1:
        return avs[0], db.get(avs[0])
    if len(avs) > 1:
        wanted = str(name).replace("/", "\\").lower()
        hits = []
        for av in avs:
            entry = db.get(av) or {}
            for path in entry.get("paths") or []:
                if str(path).replace("/", "\\").lower().endswith(wanted):
                    hits.append(av)
                    break
        if len(hits) == 1:
            return hits[0], db.get(hits[0])
    return None, None


# ---------------------------------------------------------------------------
# 联网查询
# ---------------------------------------------------------------------------
_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}


def query_by_autov2(av, api_base=API_BASE_DEFAULT, timeout=20, retries=3,
                    cancel=None):
    """查询单个 AutoV2，返回 (entry, error)。

    error = {"kind": not_found|error|no_air, "message": str,
             "http": int|None, "retry_after": float|None}
    """
    base = (api_base or API_BASE_DEFAULT).rstrip("/")
    url = "{}/api/v1/model-versions/by-hash/{}".format(base, av)
    last = {"kind": "error", "message": "未知错误", "http": None,
            "retry_after": None}
    attempts = max(1, int(retries) + 1)

    for attempt in range(attempts):
        _check_cancel(cancel)
        try:
            request = urllib.request.Request(url, headers=_API_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            try:
                data = json.loads(raw)
            except Exception as exc:
                return None, {"kind": "error",
                              "message": "响应不是 JSON：{}".format(exc),
                              "http": 200, "retry_after": None}
            if not isinstance(data, dict):
                return None, {"kind": "error", "message": "响应不是 JSON 对象",
                              "http": 200, "retry_after": None}
            air = data.get("air")
            if not air:
                return None, {"kind": "no_air",
                              "message": "API 返回了数据但没有 air 字段",
                              "http": 200, "retry_after": None}
            model = data.get("model") or {}
            mid = data.get("modelId")
            vid = data.get("id")
            entry = {
                "file": [], "paths": [],
                "modelName": model.get("name", ""),
                "AIR": air,
                "modelId": mid,
                "modelVersionId": vid,
                "url": ("https://civitai.red/models/{}?modelVersionId={}".format(mid, vid)
                        if mid and vid else ""),
                "type": model.get("type"),
                "baseModel": data.get("baseModel"),
                "baseModelType": data.get("baseModelType"),
                "queriedAt": now_str(),
                "failed": False,
                "error": None,
                "failedKind": None,
                "failedCount": 0,
                "lastTriedAt": now_str(),
                "ignored": False,
            }
            return entry, None
        except urllib.error.HTTPError as exc:
            code = exc.code
            retry_after = None
            try:
                header = exc.headers.get("Retry-After") if exc.headers else None
                if header:
                    retry_after = float(header)
            except Exception:
                retry_after = None
            if code == 404:
                return None, {"kind": "not_found",
                              "message": "HTTP 404：Civitai 未收录该哈希",
                              "http": code, "retry_after": None}
            if code in (401, 403):
                return None, {"kind": "error",
                              "message": "HTTP {}：鉴权/域名问题（检查 API 地址）".format(code),
                              "http": code, "retry_after": None}
            if code == 429:
                last = {"kind": "error",
                        "message": "HTTP 429：请求过快",
                        "http": code, "retry_after": retry_after}
                if attempt < attempts - 1:
                    _sleep(retry_after or 2.0 * (attempt + 1), cancel)
                    continue
                return None, last
            if 500 <= code < 600 and attempt < attempts - 1:
                last = {"kind": "error", "message": "HTTP {}：服务端错误".format(code),
                        "http": code, "retry_after": None}
                _sleep(1.0 * (attempt + 1), cancel)
                continue
            return None, {"kind": "error", "message": "HTTP {}：{}".format(code, exc),
                          "http": code, "retry_after": retry_after}
        except Exception as exc:
            last = {"kind": "error",
                    "message": "{}: {}".format(type(exc).__name__, exc),
                    "http": None, "retry_after": None}
            if attempt < attempts - 1:
                _sleep(1.0 * (attempt + 1), cancel)
                continue
            return None, last
    return None, last


def select_query_keys(db, mode="new", selected=None):
    keys = []
    for av, entry in (db or {}).items():
        if not isinstance(entry, dict) or is_pending_key(av):
            continue
        if entry.get("AIR") and not entry.get("failed"):
            continue
        if mode == "selected":
            if selected is None or av not in selected:
                continue
            keys.append(av)
            continue
        if entry.get("ignored"):
            continue
        kind = entry.get("failedKind")
        if mode == "new":
            if kind is None and not entry.get("queriedAt"):
                keys.append(av)
        elif mode == "not_found":
            if kind == "not_found":
                keys.append(av)
        elif mode == "failed":
            if kind in ("error", "no_air"):
                keys.append(av)
        else:
            raise ValueError("未知查询模式：{}".format(mode))
    keys.sort(key=lambda av: str((db.get(av) or {}).get("file") or av).lower())
    return keys


def query_records(db, mode="new", api_base=API_BASE_DEFAULT, timeout=20,
                  retries=3, delay=1.0, limit=0, max_consecutive_429=3,
                  selected=None, cancel=None, progress=None, log=None,
                  save_cb=None, save_every=20):
    """按状态查询；返回统计。

    mode: new / not_found / failed / selected
    """
    keys = select_query_keys(db, mode=mode, selected=selected)
    if limit and limit > 0:
        keys = keys[:limit]
    total = len(keys)
    summary = {"mode": mode, "total": total, "ok": 0, "not_found": 0,
               "error": 0, "no_air": 0, "stopped": False}
    if not total:
        _log(log, "[查询] 没有符合条件的记录。")
        return summary

    _log(log, "[查询] 模式={}，共 {} 条。".format(mode, total))
    consecutive_429 = 0
    for i, av in enumerate(keys, 1):
        _check_cancel(cancel)
        old = db.get(av) or {}
        old_files = list(old.get("file") or [])
        old_paths = list(old.get("paths") or [])
        old_kind = old.get("failedKind")

        entry, error = query_by_autov2(av, api_base=api_base, timeout=timeout,
                                       retries=retries, cancel=cancel)
        if entry:
            entry["file"] = old_files
            entry["paths"] = old_paths
            entry["queriedAt"] = now_str()
            entry["lastTriedAt"] = entry["queriedAt"]
            entry["failed"] = False
            entry["failedKind"] = None
            entry["error"] = None
            entry["failedCount"] = 0
            entry["ignored"] = bool(old.get("ignored"))
            db[av] = entry
            summary["ok"] += 1
            _log(log, "    [{}/{}] OK {} {}".format(i, total, av, entry.get("modelName")))
            consecutive_429 = 0
            status = "ok"
        else:
            kind = error.get("kind") or "error"
            message = error.get("message") or "未知错误"
            # 已确认 404 的记录，重查时遇到网络错误不降级
            if old_kind == "not_found" and kind != "not_found":
                kind = "not_found"
                message = "重查失败：{}".format(message)
            entry = old
            entry["failed"] = True
            entry["failedKind"] = kind
            entry["error"] = message
            entry["queriedAt"] = now_str()
            entry["lastTriedAt"] = entry["queriedAt"]
            entry["failedCount"] = int(entry.get("failedCount") or 0) + 1
            db[av] = entry
            if kind == "not_found":
                summary["not_found"] += 1
            elif kind == "no_air":
                summary["no_air"] += 1
            else:
                summary["error"] += 1
            _log(log, "    [{}/{}] {} {}：{}".format(i, total, kind, av, message))
            status = kind
            if error.get("http") == 429:
                consecutive_429 += 1
                if consecutive_429 >= max_consecutive_429:
                    _log(log, "[查询] 连续 429，暂停本批；剩余记录保持原状态。")
                    summary["stopped"] = True
                    break
            else:
                consecutive_429 = 0

        _progress(progress, i, total, "{} {}".format(av, status))
        if save_cb and i % max(1, save_every) == 0:
            save_cb(db)
        if i < total:
            _sleep(delay, cancel)

    if save_cb:
        save_cb(db)
    _log(log, "[查询] 完成：成功 {}，未找到 {}，失败 {}，无 AIR {}。".format(
        summary["ok"], summary["not_found"], summary["error"], summary["no_air"]))
    return summary


# ---------------------------------------------------------------------------
# 扫描与登记
# ---------------------------------------------------------------------------
def find_model_files(folder, extensions=MODEL_EXTENSIONS_DEFAULT, recursive=True):
    folder = norm_path(folder)
    if not folder or not os.path.isdir(folder):
        return []
    exts = tuple(extensions or ())
    files = []
    if recursive:
        visited = set()
        for dirpath, _dirnames, filenames in os.walk(folder, followlinks=True,
                                                     onerror=lambda _e: None):
            try:
                real = os.path.realpath(dirpath)
            except OSError:
                real = dirpath
            if real in visited:
                continue
            visited.add(real)
            for name in filenames:
                if name.lower().endswith(exts):
                    files.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and name.lower().endswith(exts):
                files.append(path)
    return sorted(files)


def _pending_entry(db, path):
    av = pending_key_for_path(path)
    name = os.path.basename(path)
    return av, {
        "file": [_unique_file_name(db, av, name)],
        "paths": [norm_path(path)],
        "modelName": "",
        "AIR": None,
        "modelId": None,
        "modelVersionId": None,
        "url": None,
        "type": None,
        "baseModel": None,
        "baseModelType": None,
        "queriedAt": None,
        "failed": False,
        "error": None,
        "failedKind": None,
        "failedCount": 0,
        "lastTriedAt": None,
        "ignored": False,
    }


def _index_entry(index, av, entry):
    if not isinstance(entry, dict):
        return
    for path in entry.get("paths") or []:
        if path:
            index["paths"].setdefault(norm_key(path), av)
            index["names"].setdefault(os.path.basename(str(path).replace("/", "\\")).lower(),
                                      []).append(av)
    for name in entry.get("file") or []:
        if name:
            index["names"].setdefault(os.path.basename(str(name).replace("/", "\\")).lower(),
                                      []).append(av)


def scan_register_files(files, db, cancel=None, progress=None, log=None,
                        save_cb=None):
    """按文件名登记/补录路径；同名不同路径先登记 pending，等哈希按内容合并。"""
    normalize_db(db)
    index = {"paths": {}, "names": {}}
    for av, entry in db.items():
        _index_entry(index, av, entry)
    for key, values in index["names"].items():
        index["names"][key] = list(dict.fromkeys(values))

    total = len(files)
    summary = {"total": total, "created": 0, "filled": 0, "skipped": 0,
               "conflicts": 0, "errors": 0}
    changed = False

    for i, path in enumerate(files, 1):
        _check_cancel(cancel)
        path = norm_path(path)
        key = path.lower()
        if key in index["paths"]:
            summary["skipped"] += 1
            _progress(progress, i, total, "已存在 {}".format(os.path.basename(path)))
            continue

        base = os.path.basename(path).lower()
        candidates = list(dict.fromkeys(index["names"].get(base, [])))
        created = False
        if not candidates:
            av, entry = _pending_entry(db, path)
            if av not in db:
                db[av] = entry
                _index_entry(index, av, entry)
                summary["created"] += 1
                changed = True
                created = True
                _log(log, "[扫描] 新建待哈希记录：{}".format(path))
        elif len(candidates) == 1:
            av = candidates[0]
            entry = db.get(av)
            if not isinstance(entry, dict):
                summary["errors"] += 1
                _log(log, "[扫描] 记录类型错误，跳过：{}".format(av))
            else:
                old_paths = list(entry.get("paths") or [])
                if not old_paths:
                    entry["paths"] = [path]
                    entry["file"] = list(entry.get("file") or [])
                    if not entry["file"]:
                        entry["file"].append(_unique_file_name(db, av,
                                                               os.path.basename(path)))
                    _index_entry(index, av, entry)
                    summary["filled"] += 1
                    changed = True
                    _log(log, "[扫描] 补路径：{}".format(path))
                elif any(norm_key(p) == key for p in old_paths):
                    summary["skipped"] += 1
                else:
                    missing = [p for p in old_paths if not os.path.exists(p)]
                    if missing:
                        entry["paths"] = [p for p in old_paths if os.path.exists(p)]
                        entry["paths"].append(path)
                        if os.path.basename(path) not in (entry.get("file") or []):
                            entry.setdefault("file", []).append(
                                _unique_file_name(db, av, os.path.basename(path)))
                        _index_entry(index, av, entry)
                        summary["filled"] += 1
                        changed = True
                        _log(log, "[扫描] 旧路径失效，已补录：{}".format(path))
                    else:
                        av2, entry2 = _pending_entry(db, path)
                        if av2 not in db:
                            db[av2] = entry2
                            _index_entry(index, av2, entry2)
                            summary["created"] += 1
                            summary["conflicts"] += 1
                            changed = True
                            created = True
                            _log(log, "[扫描] 同名不同路径，登记为待哈希，"
                                      "增量哈希后按内容合并：{}".format(path))
        else:
            av2, entry2 = _pending_entry(db, path)
            if av2 not in db:
                db[av2] = entry2
                _index_entry(index, av2, entry2)
                summary["created"] += 1
                summary["conflicts"] += 1
                changed = True
                created = True
                _log(log, "[扫描] 多个同名记录，登记为待哈希：{}".format(path))

        if not created and not changed:
            pass
        _progress(progress, i, total, os.path.basename(path))

    if changed and save_cb:
        save_cb(db)
    _log(log, "[扫描] 完成：新建 {}，补路径 {}，已存在 {}，冲突待哈希 {}，错误 {}。".format(
        summary["created"], summary["filled"], summary["skipped"],
        summary["conflicts"], summary["errors"]))
    return summary


def scan_roots(roots, db, extensions=MODEL_EXTENSIONS_DEFAULT,
               recursive=True, cancel=None, progress=None, log=None,
               save_cb=None):
    """扫描多个根目录；roots 可以是路径或 (label, path)。"""
    all_files = []
    seen = set()
    for item in roots or []:
        if isinstance(item, (tuple, list)):
            path = item[-1]
        else:
            path = item
        for file_path in find_model_files(path, extensions, recursive):
            key = norm_key(file_path)
            if key not in seen:
                seen.add(key)
                all_files.append(file_path)
    all_files.sort()
    return scan_register_files(all_files, db, cancel=cancel, progress=progress,
                               log=log, save_cb=save_cb)


def verify_local_paths(db, cancel=None, progress=None, log=None, save_cb=None):
    """清除失效路径；保留 file，便于之后扫描按文件名补录。"""
    total = len(db)
    changed = False
    summary = {"total": total, "cleared": 0, "no_paths": 0, "mismatch": 0,
               "filled_files": 0}
    for i, (av, entry) in enumerate(list(db.items()), 1):
        _check_cancel(cancel)
        if not isinstance(entry, dict):
            continue
        paths = entry.get("paths") or []
        if not paths:
            summary["no_paths"] += 1
            _progress(progress, i, total, "{} 无路径".format(av))
            continue
        new_paths = []
        for path in paths:
            if path and os.path.exists(path):
                new_paths.append(path)
            elif path:
                summary["cleared"] += 1
                changed = True
                _log(log, "[校验] 清除失效路径：{}".format(path))
        if new_paths != paths:
            entry["paths"] = new_paths
        files = entry.get("file") or []
        if not files:
            for path in new_paths:
                name = os.path.basename(path)
                if name:
                    entry.setdefault("file", []).append(
                        _unique_file_name(db, av, name))
                    summary["filled_files"] += 1
                    changed = True
        else:
            for path in new_paths:
                name = os.path.basename(path)
                if name and name not in files:
                    summary["mismatch"] += 1
                    _log(log, "[校验] file 与路径文件名不一致：{}".format(path))
        _progress(progress, i, total, av)

    if changed and save_cb:
        save_cb(db)
    _log(log, "[校验] 完成：清除失效路径 {}，无路径 {}，文件名不一致 {}，补文件名 {}。".format(
        summary["cleared"], summary["no_paths"], summary["mismatch"],
        summary["filled_files"]))
    return summary


# ---------------------------------------------------------------------------
# 哈希校验
# ---------------------------------------------------------------------------
def _merge_entries(db, target_av, source_av):
    target = db[target_av]
    source = db[source_av]
    target_files = list(target.get("file") or [])
    for name in source.get("file") or []:
        if name and name not in target_files:
            target_files.append(_unique_file_name(db, target_av, name))
    target["file"] = target_files

    target_paths = list(target.get("paths") or [])
    seen = {norm_key(p) for p in target_paths if p}
    for path in source.get("paths") or []:
        if path and norm_key(path) not in seen:
            target_paths.append(path)
            seen.add(norm_key(path))
    target["paths"] = target_paths

    if not target.get("AIR") and source.get("AIR"):
        for key in ("AIR", "modelName", "modelId", "modelVersionId", "url",
                    "type", "baseModel", "baseModelType", "queriedAt",
                    "failed", "error", "failedKind", "failedCount",
                    "lastTriedAt"):
            if key in source:
                target[key] = source[key]
    del db[source_av]


def compute_hashes(db, hash_cache, full=False, force=False, cancel=None,
                   progress=None, log=None, save_cb=None, save_every=10):
    """full=False 只处理 pending；full=True 校验所有有 paths 的记录。"""
    keys = [av for av in list(db)
            if full or is_pending_key(av)]
    total = len(keys)
    summary = {"total": total, "hashed": 0, "converted": 0, "merged": 0,
               "errors": 0, "skipped": 0}
    changed = False

    for i, av in enumerate(keys, 1):
        _check_cancel(cancel)
        entry = db.get(av)
        if not isinstance(entry, dict):
            summary["errors"] += 1
            _progress(progress, i, total, "{} 类型错误".format(av))
            continue
        paths = entry.get("paths") or []
        if not paths:
            summary["errors"] += 1
            _log(log, "[哈希] {} 没有本地路径，跳过。".format(av))
            _progress(progress, i, total, "{} 无路径".format(av))
            continue

        hashes = []
        for path in paths:
            if not path or not os.path.exists(path):
                summary["errors"] += 1
                _log(log, "[哈希] 路径不存在：{}".format(path))
                continue
            try:
                digest = hash_cache.compute(path, cancel=cancel, force=force)
                hashes.append(autov2(digest))
            except Cancelled:
                raise
            except Exception as exc:
                summary["errors"] += 1
                _log(log, "[哈希] 计算失败 {}：{}".format(path, exc))

        if not hashes:
            _progress(progress, i, total, "{} 失败".format(av))
            continue
        if len(set(hashes)) > 1:
            summary["errors"] += 1
            _log(log, "[哈希] {} 的多个路径哈希不一致，跳过。".format(av))
            _progress(progress, i, total, "{} 哈希不一致".format(av))
            continue

        real_av = hashes[0]
        if is_pending_key(av):
            if real_av in db and real_av != av:
                _merge_entries(db, real_av, av)
                summary["merged"] += 1
                changed = True
                _log(log, "[哈希] 临时记录已合并到 {}".format(real_av))
            else:
                db[real_av] = db.pop(av)
                summary["converted"] += 1
                changed = True
                _log(log, "[哈希] 临时记录转为正式 AutoV2：{}".format(real_av))
            summary["hashed"] += 1
        else:
            if av != real_av:
                summary["errors"] += 1
                _log(log, "[哈希] 主键 {} 与文件哈希 {} 不一致。".format(av, real_av))
            else:
                summary["hashed"] += 1
                _log(log, "[哈希] {} 校验通过".format(av))

        _progress(progress, i, total, "{} -> {}".format(av, real_av))
        if changed and save_cb and i % max(1, save_every) == 0:
            save_cb(db)

    try:
        hash_cache.save()
    except Exception as exc:
        _log(log, "[哈希] 缓存保存失败：{}".format(exc))
    if changed and save_cb:
        save_cb(db)
    _log(log, "[哈希] 完成：处理 {}，转换 {}，合并 {}，错误 {}。".format(
        summary["hashed"], summary["converted"], summary["merged"],
        summary["errors"]))
    return summary


# ---------------------------------------------------------------------------
# 模型索引（PNG 注入 / 查询节点用）
# ---------------------------------------------------------------------------
KIND_ROOT_HINTS = {
    "lora": ("lora", "loras"),
    "checkpoint": ("checkpoint", "checkpoints", "diffusion_models", "unet"),
    "diffusionmodel": ("diffusion_models", "unet", "checkpoints"),
    "controlnet": ("controlnet", "control_net"),
    "vae": ("vae",),
    "textencoder": ("text_encoders", "clip", "textencoder"),
    "upscaler": ("upscale_models", "upscaler"),
}


class ModelIndex:
    """按类别根目录建立 相对路径 / 文件名 索引。"""

    def __init__(self, extensions=BROAD_MODEL_EXTENSIONS):
        self.extensions = parse_extensions(extensions)
        self.by_rel = {}
        self.by_base = {}
        self.root_of = {}
        self.files = []
        self._seen = set()

    def add_root(self, root, label=""):
        root = norm_path(root)
        if not root or not os.path.isdir(root):
            return 0
        added = 0
        visited = set()
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True,
                                                     onerror=lambda _e: None):
            try:
                real = os.path.realpath(dirpath)
            except OSError:
                real = dirpath
            if real in visited:
                continue
            visited.add(real)
            for name in filenames:
                if not name.lower().endswith(self.extensions):
                    continue
                full = os.path.join(dirpath, name)
                key = norm_key(full)
                if key in self._seen:
                    continue
                self._seen.add(key)
                self.files.append(full)
                rel = os.path.relpath(full, root).replace("/", "\\").lower()
                self.by_rel.setdefault(rel, []).append(full)
                self.by_base.setdefault(name.lower(), []).append(full)
                self.root_of[full] = label
                added += 1
        return added

    def find(self, name, kind=None):
        if not name:
            return None
        wanted = str(name).replace("/", "\\").lstrip("\\").lower()
        base = os.path.basename(wanted)
        exact = list(self.by_rel.get(wanted, []))
        candidates = exact or list(self.by_base.get(base, []))
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        hints = KIND_ROOT_HINTS.get(kind or "", ())
        if hints:
            filtered = [p for p in candidates
                        if any(h in (self.root_of.get(p, "").lower()) for h in hints)]
            if len(filtered) == 1:
                return filtered[0]
            if filtered:
                candidates = filtered
        suffix_hits = [p for p in candidates
                       if p.replace("/", "\\").lower().endswith(wanted)]
        if len(suffix_hits) == 1:
            return suffix_hits[0]
        if suffix_hits:
            candidates = suffix_hits
        return sorted(candidates)[0]
