"""Private server-to-server adapter; never forward the RAG user's token."""
import os
import re
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException

MAX_SOURCE = 40 * 1024 * 1024
MAX_RESULT = 100 * 1024 * 1024
FORMATS = {'pdf', 'docx', 'txt', 'md', 'png', 'jpg', 'jpeg', 'webp'}
INSTRUCTION = ('根据上传原件制作完整阅读版H5，自动使用原文件名或原文标题。保留全部实质章节、事实、限定条件、'
               '表格、正文原图和来源关系；保持V9完整首页与主题设计，使用已有通用图解流程。'
               '不要制作摘要版，不要把原文仅放在附件中代替正文覆盖。')


def enabled():
    return os.getenv('H5_WORKBENCH_ENABLED', '').lower() in {'1', 'true'}


def config():
    address = os.getenv('H5_PAGEO_URL', '').rstrip('/')
    token = os.getenv('H5_PAGEO_SERVICE_KEY', '')
    try:
        parsed = urlsplit(address)
        parsed.port
    except ValueError:
        raise HTTPException(503, 'H5 服务地址无效，请联系管理员。') from None
    if (not enabled() or parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'} or not token or '\n' in token or '\r' in token):
        raise HTTPException(503, 'H5 生成服务尚未配置，请联系管理员。')
    return address, token


class PageoClient:
    async def __aenter__(self):
        address, token = config()
        self.client = httpx.AsyncClient(base_url=address, headers={'X-API-Key': token},
            timeout=httpx.Timeout(90, connect=5, pool=5), follow_redirects=False, trust_env=False)
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def request(self, method, path, **kwargs):
        # The path is entirely application-controlled; never proxy arbitrary URLs.
        if not re.fullmatch(r'/api/h5/(?:themes|reports(?:/[a-zA-Z0-9-]+(?:/(?:sources|theme|chat|continue|repair-chapter|preview|download|static\.zip))?)?)', path):
            raise ValueError('Unsupported Pageo operation')
        async with self.client.stream(method, path, **kwargs) as response:
            if response.status_code >= 300:
                raise HTTPException(503 if response.status_code >= 500 else 409,
                    f'H5 后台返回 HTTP {response.status_code}，已保留进度，请检查服务或任务状态。')
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESULT:
                    raise HTTPException(413, 'H5 结果超过当前大小上限。')
                chunks.append(chunk)
            payload = b''.join(chunks)
            if path.endswith(('/preview', '/download', '/static.zip')):
                return payload
            import json
            return json.loads(payload)
