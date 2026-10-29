"""Bound the multipart body before Starlette spools uploaded files to disk."""
from fastapi import HTTPException
from starlette.responses import JSONResponse

from app.config.settings import settings


class ImageUploadLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST' or scope['path'] != '/api/v1/attachments':
            return await self.app(scope, receive, send)
        limit = settings.media_max_bytes + 128 * 1024
        headers = dict(scope.get('headers', []))
        try:
            length = int(headers.get(b'content-length', b'0'))
        except ValueError:
            length = limit + 1
        if length > limit:
            return await JSONResponse({'detail': '图片上传请求过大'}, status_code=413)(scope, receive, send)
        size = 0
        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get('body', b''))
            if size > limit:
                raise HTTPException(413, '图片上传请求过大')
            return message
        await self.app(scope, limited_receive, send)
