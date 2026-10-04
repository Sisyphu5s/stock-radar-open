"""/api/v1/market/stream : 行情 SSE 事件流(关注列表 + 沪深300 快照子集推送)。

事件协议(与前端 src/api/marketStream.ts 类型契约对齐):
- event: snapshot  全量子集快照(首连/新连接经 ring 速发,data 含 type/ts/source/count/items);
- event: tick      增量(阈值过滤后的变化行,data = {type, ts, items[]});
- event: signal    自选股新信号事件增量(data = {type, ts, events[]},字段契约同
  /market/watchlist/notifications);
- 15s 心跳保活(: heartbeat comment);慢消费者(队列积压 > core.market_stream.MAX_QUEUED)
  断开连接,客户端 EventSource 自动重连后经 ring 速发最近快照。

推送核心在 core/market_stream.py(后台线程生成事件),本模块只负责 SSE 流封装:
首连 snapshot(ring)→ 实时事件 → 心跳 → 慢消费断开;客户端断开时生成器被取消,
finally 中 unsubscribe 清理订阅(与 api/experiments._event_stream 同款收尾)。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..core import market_stream as ms

logger = logging.getLogger("stockradar.market_stream")

router = APIRouter(prefix="/market", tags=["market"])

# SSE 心跳间隔(秒):队列空时按此时长阻塞等待,超时发心跳保活。
# 测试可 monkeypatch 为小值以缩短空等。
_SSE_GET_TIMEOUT = 15


async def _stream(hub: ms.MarketStreamHub | None = None):
    """SSE 异步生成器:首连速发最近快照(ring)→ 实时事件 → 心跳 → 慢消费断开。

    事件格式与旧版 sync 生成器逐字节一致(event: snapshot/tick/signal + 心跳);
    订阅走 hub.asubscribe(AsyncSubQueue 线程→事件循环桥),后台推送线程
    put_nowait 经 loop.call_soon_threadsafe 入队,生成器 await 消费——
    长连接不再占用 anyio 线程池线程;推送核心(取数/diff/广播)仍在后台线程。
    """
    hub = hub or ms.hub
    sub_id, q = hub.asubscribe()
    try:
        snap = hub.snapshot()
        if snap is not None:
            yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
        while True:
            try:
                e = await asyncio.wait_for(q.get(), timeout=_SSE_GET_TIMEOUT)
                if q.qsize() > ms.MAX_QUEUED:
                    # 慢消费者:生产远快于消费(积压超阈值)→ 断开,客户端 EventSource
                    # 自动重连后经 ring 速发最近快照,积压自然清空。
                    logger.warning("行情 SSE 慢消费者积压 %d 条,断开连接", q.qsize())
                    return
                yield (
                    f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                )
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        hub.unsubscribe(sub_id)


@router.get("/stream")
async def market_stream():
    """行情事件流:event: snapshot / tick / signal + 15s 心跳(推送数据源=现有三源降级链路)。"""
    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
