"""
知识库图片同源代理

"""
from __future__ import annotations

import urllib.parse
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from minio.error import S3Error

from app.core.logger import logger
from app.rag.clients.minio_client import get_minio_client
from app.rag.conf.minio_config import minio_config

router = APIRouter()


def _resolve_object_key(raw: str) -> str | None:
    """
    校验图片地址是否来自本库 MinIO（fail-closed），通过则返回对象 key。
    """
    if not raw or not minio_config.endpoint:
        return None
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() != minio_config.endpoint.lower():
        return None
    prefix = f"/{minio_config.bucket_name}/"
    if not parsed.path.startswith(prefix):
        return None
    key = parsed.path[len(prefix):].lstrip("/")
    return key or None


def _stream_object(bucket: str, key: str) -> Iterator[bytes]:
    """服务端读取 MinIO 对象并流式产出（结束必定释放连接）。"""
    client = get_minio_client()
    resp = client.get_object(bucket, key)
    try:
        for chunk in resp.stream(64 * 1024):
            yield chunk
    finally:
        resp.close()
        resp.release_conn()


@router.get("/api/image")
async def proxy_image(src: str = Query(..., description="原始 MinIO 图片地址")):
    key = _resolve_object_key(src)
    if not key:
        raise HTTPException(status_code=400, detail="非本库图片地址，已拒绝代理")

    try:
        resp = get_minio_client().get_object(minio_config.bucket_name, key)
    except S3Error as exc:
        logger.warning(
            f"[image-proxy] 取图失败 bucket={minio_config.bucket_name} key={key}: {exc}"
        )
        raise HTTPException(status_code=404, detail="图片不存在或无权访问")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[image-proxy] 取图异常 key={key}: {exc}")
        raise HTTPException(status_code=502, detail="图片服务异常")

    content_type = resp.headers.get("Content-Type") or "application/octet-stream"

    def _stream() -> Iterator[bytes]:
        try:
            for chunk in resp.stream(64 * 1024):
                yield chunk
        finally:
            resp.close()
            resp.release_conn()

    return StreamingResponse(
        _stream(),
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
