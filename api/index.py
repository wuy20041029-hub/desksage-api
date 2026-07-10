"""Vercel Serverless Function 入口"""
from main import app as fastapi_app

class PathRewriteMiddleware:
    """处理 Vercel 路由：把 /api/index/xxx 转换为 /xxx"""
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            # Vercel rewrite 后路径可能变成 /api/index
            # 我们需要把 /api/xxx 的请求正确映射
            # FastAPI 路由是 /health, /verify-key 等
            # Vercel 把 /api/health rewrite 到 /api/index
            # 但 FastAPI 收到的 path 可能是 /api/index 或 /api/health
            # 如果是 /api/index，映射到 /
            if path == "/api/index" or path == "/api/index/":
                scope["path"] = "/health"  # 默认路由
            # 如果路径以 /api/ 开头，去掉 /api 前缀
            elif path.startswith("/api/"):
                scope["path"] = path[4:]  # 去掉 /api
                if "raw_path" in scope:
                    raw = scope["raw_path"]
                    if isinstance(raw, bytes):
                        scope["raw_path"] = raw[4:]
            # 如果路径是 /api，映射到 /health
            elif path == "/api":
                scope["path"] = "/health"
        await self.app(scope, receive, send)

app = PathRewriteMiddleware(fastapi_app)