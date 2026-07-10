"""Vercel Serverless Function 入口"""
from main import app as fastapi_app

class StripApiPrefixMiddleware:
    """去掉 /api 前缀，让 FastAPI 路由正确匹配"""
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/api/"):
                scope["path"] = path[4:]  # 去掉 "/api"
                if "raw_path" in scope:
                    raw = scope["raw_path"]
                    if isinstance(raw, bytes) and raw.startswith(b"/api/"):
                        scope["raw_path"] = raw[4:]
            elif path == "/api":
                scope["path"] = "/"
                if "raw_path" in scope:
                    raw = scope["raw_path"]
                    if isinstance(raw, bytes):
                        scope["raw_path"] = b"/"
        await self.app(scope, receive, send)

app = StripApiPrefixMiddleware(fastapi_app)