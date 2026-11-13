# -*- coding: utf-8 -*-
"""civitai_db 的 HTTP 接口：面板页面、CRUD、任务、体检、文件夹选择。

路由注册在 ComfyUI 的 PromptServer 上，和 ComfyUI 同进程、同端口。
没有 ComfyUI 时（单元测试 / 独立运行）只定义函数，不注册路由。
"""

from __future__ import annotations

import os
import subprocess
import threading

try:
    from . import core
    from . import service as service_mod
except ImportError:  # 允许在包外单独 import（单元测试）
    import core
    import service as service_mod

try:
    from aiohttp import web
except Exception:  # pragma: no cover
    web = None

try:
    from server import PromptServer
    _ROUTES = PromptServer.instance.routes if PromptServer.instance else None
except Exception:  # pragma: no cover
    _ROUTES = None

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(MODULE_DIR, "ui")

_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = service_mod.CivitaiService()
        return _service


def _local_only(request):
    service = get_service()
    if service.config.get("allow_remote"):
        return True
    remote = request.remote or ""
    return (remote in ("127.0.0.1", "::1", "localhost", "")
            or remote.startswith("127."))


def _guard(request):
    if web is None:
        return None
    if not _local_only(request):
        return web.json_response({"ok": False, "error": "仅允许本机访问"},
                                 status=403)
    return None


def _error(message, status=400):
    return web.json_response({"ok": False, "error": str(message)}, status=status)


async def _body(request):
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def register():
    """在 PromptServer 上注册路由；重复调用安全。"""
    if _ROUTES is None or web is None:
        return False
    if getattr(register, "_done", False):
        return True
    register._done = True
    routes = _ROUTES

    @routes.get("/db")
    async def civitai_db_no_slash(request):
        target = "/db/"
        if request.query_string:
            target += "?" + request.query_string
        raise web.HTTPPermanentRedirect(target)

    @routes.get("/civitai-db{tail:.*}")
    async def civitai_db_legacy_redirect(request):
        # 旧地址永久跳到更短的新地址，保留查询参数
        tail = (request.match_info.get("tail") or "").lstrip("/")
        target = "/db/" + tail
        if request.query_string:
            target += "?" + request.query_string
        raise web.HTTPPermanentRedirect(target)

    @routes.get("/db/")
    async def civitai_db_index(request):
        index = os.path.join(UI_DIR, "index.html")
        if not os.path.isfile(index):
            return web.Response(status=404, text="面板文件不存在")
        return web.FileResponse(index)

    if os.path.isdir(UI_DIR):
        routes.static("/db/static", UI_DIR)

    @routes.get("/db/state")
    async def civitai_db_state(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        return web.json_response({"ok": True, "state": get_service().snapshot()})

    @routes.get("/db/entries")
    async def civitai_db_entries(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        svc = get_service()
        return web.json_response({
            "ok": True,
            "rows": svc.database.entries(),
            "stats": svc.database.stats(),
        })

    @routes.get("/db/entry/{av:.+}")
    async def civitai_db_get_entry(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        av = request.match_info["av"]
        entry = get_service().database.get_entry(av)
        if entry is None:
            return _error("记录不存在", 404)
        return web.json_response({"ok": True, "key": av, "entry": entry})

    @routes.put("/db/entry/{av:.+}")
    async def civitai_db_put_entry(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        av = request.match_info["av"]
        body = await _body(request)
        entry = body.get("entry", body)
        try:
            saved = get_service().database.put_entry(av, entry)
        except (ValueError, core.DBError) as exc:
            return _error(exc)
        return web.json_response({"ok": True, "key": av, "entry": saved})

    @routes.delete("/db/entry/{av:.+}")
    async def civitai_db_delete_entry(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        av = request.match_info["av"]
        try:
            removed = get_service().database.delete_entry(av)
        except core.DBError as exc:
            return _error(exc)
        if not removed:
            return _error("记录不存在", 404)
        return web.json_response({"ok": True, "key": av})

    @routes.post("/db/jobs")
    async def civitai_db_start_job(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        body = await _body(request)
        action = body.get("action")
        params = body.get("params") or {}
        allowed = {"scan", "verify", "hash_inc", "hash_full", "query_new",
                   "query_not_found", "query_failed", "query_selected",
                   "inject"}
        if action not in allowed:
            return _error("未知动作：{}".format(action))
        try:
            job_id = get_service().jobs.start(action, params)
        except Exception as exc:
            return _error(exc, 409)
        return web.json_response({"ok": True, "job_id": job_id})

    @routes.get("/db/jobs/{job_id}")
    async def civitai_db_job(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        job = get_service().jobs.snapshot()
        if not job or job["id"] != request.match_info["job_id"]:
            return _error("任务不存在或已结束", 404)
        return web.json_response({"ok": True, "job": job})

    @routes.post("/db/jobs/{job_id}/cancel")
    async def civitai_db_cancel(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        job = get_service().jobs.snapshot()
        if not job or job["id"] != request.match_info["job_id"]:
            return _error("任务不存在", 404)
        get_service().jobs.cancel()
        return web.json_response({"ok": True})

    @routes.get("/db/health")
    async def civitai_db_health(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        issues = get_service().database.health()
        return web.json_response({"ok": True, "issues": issues})

    @routes.get("/db/lookup")
    async def civitai_db_lookup(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        model = request.query.get("model", "")
        auto_hash = request.query.get("auto_hash", "0") in ("1", "true", "yes")
        allow_network = request.query.get("allow_network", "0") in ("1", "true", "yes")
        result = get_service().lookup(model, auto_hash=auto_hash,
                                      allow_network=allow_network)
        return web.json_response({"ok": True, "result": result})

    @routes.get("/db/config")
    async def civitai_db_get_config(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        return web.json_response({"ok": True, "config": get_service().config.data})

    @routes.put("/db/config")
    async def civitai_db_put_config(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        body = await _body(request)
        config = body.get("config", body)
        if not isinstance(config, dict):
            return _error("config 必须是 JSON 对象")
        svc = get_service()
        try:
            svc.config.update(config)
            svc.database.reload(force=True)
        except Exception as exc:
            return _error(exc)
        return web.json_response({"ok": True, "config": svc.config.data})

    @routes.get("/db/browse")
    async def civitai_db_browse(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        path = request.query.get("path") or os.path.expanduser("~")
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return _error("目录不存在：{}".format(path), 404)
        try:
            names = sorted(os.listdir(path), key=lambda n: n.lower())
        except OSError as exc:
            return _error(exc)
        dirs = [n for n in names if os.path.isdir(os.path.join(path, n))]
        parent = os.path.dirname(path)
        return web.json_response({
            "ok": True, "path": path,
            "parent": parent if parent and parent != path else "",
            "dirs": dirs,
        })

    @routes.post("/db/reveal")
    async def civitai_db_reveal(request):
        blocked = _guard(request)
        if blocked is not None:
            return blocked
        body = await _body(request)
        path = os.path.abspath(body.get("path") or "")
        if not path or not os.path.exists(path):
            return _error("路径不存在", 404)
        target = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            if os.name == "nt":
                os.startfile(target)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            return _error(exc)
        return web.json_response({"ok": True, "path": target})

    @routes.get("/db/ping")
    async def civitai_db_ping(request):
        return web.json_response({"ok": True, "name": "civitai_db"})

    return True
