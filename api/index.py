"""
Vercel Serverless Function 入口
添加路径前缀适配，解决 Vercel 自动去掉 /api 前缀的问题
"""
from main import app as original_app

class PathPrefixMiddleware:
    """ASGI 中间件：给路径加上 /api 前缀"""
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith("/api"):
            scope["path"] = "/api" + scope["path"]
            # 同时修正 raw_path
            if "raw_path" in scope:
                scope["raw_path"] = b"/api" + scope["raw_path"]
        await self.app(scope, receive, send)

app = PathPrefixMiddleware(original_app)