#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件索引与管理工具 —— 后端服务入口（FastAPI）
运行：pip install fastapi uvicorn  →  python backend.py
默认感知 http://127.0.0.1:8000
环境变量：FIT_DB(数据库路径) / FIT_HOST / FIT_PORT

代码已按职责拆分为 xray/ 包（config / scan / browse / ocr / transcribe / tasks），
本文件只负责装配 FastAPI 应用、挂载静态资源、安全中间件与启动入口。
"""
import os
import sys
import webbrowser
import atexit
import threading

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

# 领域模块（各自持有 APIRouter；共享的常量/DB/工具统一从 xray.config 导入）
from xray import config as xconfig
from xray import scan as xscan
from xray import browse as xbrowse
from xray import ocr as xocr
from xray import transcribe as xtranscribe
from xray import tasks as xtasks

# 直接使用 config 中的共享名称
BASE_DIR = xconfig.BASE_DIR
HOST = xconfig.HOST
PORT = xconfig.PORT
DB_WHERE = xconfig.DB_WHERE
DB_PATH = xconfig.DB_PATH
LOG_DIR = xconfig.LOG_DIR
SECRET_TOKEN = xconfig.SECRET_TOKEN
MAX_BODY = xconfig.MAX_BODY

app = FastAPI(title="文件索引与管理工具")

# 前端 JS 模块（web/ 目录，按职责拆分）静态伺服；不含敏感信息，无需令牌
app.mount("/web", StaticFiles(directory=os.path.join(BASE_DIR, "web")), name="web")

# ----------------------------------------------------------------------------
# 安全中间件：本地令牌校验 + 写接口请求体大小限制
# 仅对 /api/ 路径生效；页面本身（/）不受限，由后端在返回时注入令牌。
# ----------------------------------------------------------------------------
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > MAX_BODY:
                return JSONResponse(status_code=413, content={"error": "请求体过大"})
        # 本地令牌校验：前端自动携带；未设置 SECRET_TOKEN（FIT_TOKEN=""）则放行
        if SECRET_TOKEN:
            tok = request.headers.get("X-Fit-Token") or request.query_params.get("token") or ""
            if tok != SECRET_TOKEN:
                return JSONResponse(status_code=401, content={"error": "未授权：缺少或错误的访问令牌"})
    return await call_next(request)

# 装配各领域的 APIRouter（路由按模块归位，不再集中在此文件）
app.include_router(xconfig.router)
app.include_router(xscan.router)
app.include_router(xbrowse.router)
app.include_router(xocr.router)
app.include_router(xtranscribe.router)
app.include_router(xtasks.router)


# ----------------------------------------------------------------------------
# 页面根路由（仅此两处留在入口，其余 API 在 xray/ 各模块）
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    # 首页内嵌 token 占位符，运行时替换为真实令牌，供前端 fetch 自动携带（媒体预览走 ?token=）
    html_path = os.path.join(BASE_DIR, "index.html")
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("FIT_TOKEN_PLACEHOLDER", SECRET_TOKEN or "")
    return HTMLResponse(html)


@app.get("/api/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    xconfig.init_db()
    url = f"http://{HOST}:{PORT}"
    # 单实例：若已有实例在运行，直接打开浏览器并退出，避免端口冲突崩溃
    existing = xconfig._read_running_url()
    if existing and xconfig._server_alive(existing):
        print(f"检测到已在运行的服务：{existing}，直接打开浏览器。")
        try:
            webbrowser.open(existing)
        except Exception:
            pass
        sys.exit(0)
    xconfig._write_lock(url)
    atexit.register(xconfig._remove_lock)
    print(f"文件索引工具已启动: {url}")
    print(f"数据库位置（{DB_WHERE}）: {DB_PATH}")
    print(f"日志位置: {os.path.join(LOG_DIR, 'xray.log')}")
    # 启动后自动打开浏览器（打包成 exe 时更友好；无桌面环境静默忽略）
    def _open():
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Timer(1.2, _open).start()
    try:
        uvicorn.run(app, host=HOST, port=PORT)
    except OSError as _e:
        # 极端情况下端口仍被占用（如被其它无关程序占住 8000）：提示而非静默崩溃
        if "10048" in str(_e) or "address already in use" in str(_e).lower():
            print(f"端口 {PORT} 已被占用，无法启动。请先关闭占用该端口的程序，"
                  f"或设置环境变量 FIT_PORT 指定其它端口。")
        else:
            raise
    finally:
        xconfig._remove_lock()
