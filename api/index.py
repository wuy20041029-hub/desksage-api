"""
Vercel Serverless Function 入口
Vercel 会把 /api/xxx 的请求转发到这里，但 path 会变成去掉 /api 后的路径
所以需要中间件把 /api 加回去
"""
from main import app as original_app

class PathPrefixMiddleware:
    """ASGI 中间件：给路径加上 /api 前缀"""
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            # Vercel 可能把 /api/health 变成 /health，需要加回去
            if not path.startswith("/api"):
                scope["path"] = "/api" + path
                if "raw_path" in scope:
                    raw = scope["raw_path"]
                    if isinstance(raw, bytes):
                        scope["raw_path"] = b"/api" + raw
                    else:
                        scope["raw_path"] = "/api" + raw
        await self.app(scope, receive, send)

app = PathPrefixMiddleware(original_app)