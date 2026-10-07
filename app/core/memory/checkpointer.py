"""
记忆层 checkpointer：官方 Redis saver 的装配与降级

"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.checkpoint.memory import InMemorySaver

from app.core.logger import logger
from app.core.memory.config import memory_config


def _prefixes() -> tuple[str, str]:
    """
    返回 (检查点前缀, 写入前缀)，都不带末尾分隔符（官方会自己补）
    """
    base = (memory_config.ckpt_key_prefix or "dsa:ckpt:").rstrip(":")
    return base, f"{base}_write"


class DegradingCheckpointSaver(BaseCheckpointSaver):
    """
    薄委托适配器：主 saver 一旦抛异常就永久切换到进程内内存实现

    设计取舍：降级是单向且一次性的。Redis 恢复后也不切回主 saver ——
    否则同一会话的状态会在两个存储之间来回跳，造成「记忆时有时无」这种更难排查的问题。
    降级时记一次 warning，日志关键字与阶段 1 的验收口径保持一致。
    """

    def __init__(self, primary: BaseCheckpointSaver):
        # serde 沿用主 saver 的：本类不自行序列化，但 langgraph 可能读它（如 with_allowlist）
        super().__init__(serde=getattr(primary, "serde", None))
        self._primary = primary
        self._fallback: Optional[InMemorySaver] = None
        self._degraded = False

    @property
    def degraded(self) -> bool:
        """是否已退化为进程内内存实现（供运维观测与验证脚本断言）"""
        return self._degraded

    @property
    def primary(self) -> BaseCheckpointSaver:
        """被包装的官方 saver（装配与释放时要用它的 asetup 与私有连接）"""
        return self._primary

    def degrade(self, exc: BaseException) -> None:
        """切换到内存实现；只在第一次降级时记 warning，避免日志被刷屏"""
        if self._fallback is None:
            self._fallback = InMemorySaver()
        if not self._degraded:
            self._degraded = True
            logger.warning(
                "[Memory] redis 运行期不可用，checkpointer 回退 InMemorySaver（本进程后续"
                f"会话记忆不再跨进程）：{exc}"
            )

    def _target(self) -> BaseCheckpointSaver:
        return self._fallback if self._degraded else self._primary

    # ------------------------------------------------------------ 同步接口转发
    def _sync(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._degraded:
            return getattr(self._fallback, name)(*args, **kwargs)
        try:
            return getattr(self._primary, name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001  热层任何异常都不得冒到主图
            self.degrade(e)
            return getattr(self._fallback, name)(*args, **kwargs)

    def get_tuple(self, config) -> Optional[CheckpointTuple]:
        return self._sync("get_tuple", config)

    def put(self, config, checkpoint, metadata, new_versions):
        return self._sync("put", config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id: str, task_path: str = "") -> None:
        return self._sync("put_writes", config, writes, task_id, task_path)

    # ------------------------------------------------------------ 异步接口转发
    async def _async(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._degraded:
            return await getattr(self._fallback, name)(*args, **kwargs)
        try:
            return await getattr(self._primary, name)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            self.degrade(e)
            return await getattr(self._fallback, name)(*args, **kwargs)

    async def aget_tuple(self, config) -> Optional[CheckpointTuple]:
        return await self._async("aget_tuple", config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await self._async("aput", config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id: str, task_path: str = "") -> None:
        await self._async("aput_writes", config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._async("adelete_thread", thread_id)

    async def alist(
        self, config, *, filter=None, before=None, limit=None
    ) -> AsyncIterator[CheckpointTuple]:
        # 先把结果取完再往外 yield：否则异常发生在迭代过程中，
        # 那时已经吐出一半结果，降级重试就会重复投递。
        try:
            if self._degraded:
                items = [
                    x
                    async for x in self._fallback.alist(
                        config, filter=filter, before=before, limit=limit
                    )
                ]
            else:
                try:
                    items = [
                        x
                        async for x in self._primary.alist(
                            config, filter=filter, before=before, limit=limit
                        )
                    ]
                except Exception as e:  # noqa: BLE001
                    self.degrade(e)
                    items = [
                        x
                        async for x in self._fallback.alist(
                            config, filter=filter, before=before, limit=limit
                        )
                    ]
        except Exception as e:  # noqa: BLE001  连降级目标都失败时不再抛
            logger.warning(f"[Memory] checkpointer 列举检查点失败，返回空结果：{e}")
            items = []
        for item in items:
            yield item

    def get_next_version(self, current, channel):
        # 主 saver 与内存实现的版本策略各自由自己决定；这里只保证「跟随当前生效目标」，
        # 避免降级瞬间出现两套版本号混用
        return self._target().get_next_version(current, channel)


def _probe_redis_capabilities() -> None:
    """
    启动期能力探针：连接可用 + RediSearch 在线

    这两件事必须一起验：只验连接，缺模块的实例会通过；只验模块，连不上时报的是无关的错。
    """
    from app.rag.clients.manager.redis_client_manager import redis_client_manager

    if redis_client_manager.client is None:
        redis_client_manager.init()
    client = redis_client_manager.client
    if not client.ping():
        raise RuntimeError("redis ping 未返回 PONG")
    # FT._LIST 是 RediSearch 的最小探针：缺模块时直接报 unknown command
    client.execute_command("FT._LIST")


def _build_redis_saver() -> BaseCheckpointSaver:
    """按集中配置构造官方异步 saver（此处不做 setup，setup 由 lifespan 在事件循环里做）"""
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    from app.rag.conf.redis_config import redis_config

    checkpoint_prefix, write_prefix = _prefixes()
    ttl_config = None
    if memory_config.ckpt_ttl_s > 0:
        ttl_config = {
            "default_ttl": max(1, int(memory_config.ckpt_ttl_s) // 60),
            "refresh_on_read": memory_config.ckpt_refresh_on_read,
        }

    return AsyncRedisSaver(
        redis_url=redis_config.redis_url,
        connection_args={
            "socket_timeout": redis_config.socket_timeout_s,
            "socket_connect_timeout": redis_config.connect_timeout_s,
            "health_check_interval": redis_config.health_check_interval_s,
        },
        ttl=ttl_config,
        checkpoint_prefix=checkpoint_prefix,
        checkpoint_write_prefix=write_prefix,
    )


def build_checkpointer() -> BaseCheckpointSaver:
    """
    构造主智能体使用的 checkpointer

    优先官方 Redis saver（外面包一层降级适配器）；探针失败或记忆层被关闭时回退进程内内存实现。
    本函数绝不抛：主智能体在 import 期就要拿到 checkpointer，此处抛异常等于服务起不来，
    而「Redis 没起来」只应退化成改造前的行为，不该阻断启动。
    """
    if not memory_config.enabled:
        logger.info("[Memory] MEMORY_ENABLE=false，checkpointer 使用 InMemorySaver")
        return InMemorySaver()

    try:
        _probe_redis_capabilities()
        saver = DegradingCheckpointSaver(_build_redis_saver())
        checkpoint_prefix, write_prefix = _prefixes()
        ttl_desc = (
            f"{memory_config.ckpt_ttl_s}s（{max(1, memory_config.ckpt_ttl_s // 60)} 分钟）"
            if memory_config.ckpt_ttl_s > 0
            else "不过期"
        )
        logger.info(
            f"[Memory] checkpointer 使用官方 AsyncRedisSaver：前缀={checkpoint_prefix} / "
            f"{write_prefix}，TTL={ttl_desc}，读时续期={memory_config.ckpt_refresh_on_read}"
        )
        return saver
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] redis 不可用，回退 InMemorySaver：{e}")
        return InMemorySaver()


async def asetup_checkpointer(saver: Optional[BaseCheckpointSaver]) -> bool:
    """
    在事件循环里为 Redis saver 建索引（官方要求首次使用前必须 setup）

    官方 asetup 会捕获当前事件循环，同步包装方法靠它回调；索引建不起来则整个 saver 不可用，
    因此失败时直接降级（若外面套了适配器的话）。本函数仅告警，绝不抛。
    """
    target = getattr(saver, "primary", saver)
    setup = getattr(target, "asetup", None)
    if setup is None:
        return True
    try:
        await setup()
        logger.info("[Memory] checkpointer RediSearch 索引就绪")
        return True
    except Exception as e:  # noqa: BLE001
        if isinstance(saver, DegradingCheckpointSaver):
            saver.degrade(e)
        else:
            logger.warning(f"[Memory] checkpointer 索引初始化失败：{e}")
        return False


async def aclose_checkpointer(saver: Optional[BaseCheckpointSaver]) -> None:
    """释放 saver 自有的异步连接池（只关自己建的，不关外部传入的客户端）"""
    target = getattr(saver, "primary", saver)
    if not getattr(target, "_owns_its_client", False):
        return
    client = getattr(target, "_redis", None)
    if client is None:
        return
    try:
        aclose = getattr(client, "aclose", None)
        if aclose is not None:
            await aclose()
            return
        pool = getattr(client, "connection_pool", None)
        if pool is not None and hasattr(pool, "disconnect"):
            await pool.disconnect()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Memory] 释放 Redis checkpointer 连接时出现异常：{e}")


__all__ = [
    "DegradingCheckpointSaver",
    "build_checkpointer",
    "asetup_checkpointer",
    "aclose_checkpointer",
]
