import asyncio
import time
from typing import Optional

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import StreamingResponse

from app.core.auth import get_app_key, verify_app_key
from app.core.batch import create_task, expire_task, get_task
from app.core.logger import logger
from app.core.storage import get_storage
from app.services.grok.batch_services.usage import UsageService
from app.services.grok.batch_services.nsfw import NSFWService
from app.services.token.manager import get_token_manager
from app.services.token.models import TokenListResponse

router = APIRouter()

# 统计数据缓存（60秒过期）
_stats_cache = None
_stats_cache_time = 0
_stats_cache_ttl = 60


def _invalidate_stats_cache():
    global _stats_cache
    _stats_cache = None


@router.get("/tokens", dependencies=[Depends(verify_app_key)])
async def get_tokens(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    status: Optional[str] = Query(None, description="状态筛选"),
):
    """获取 Token 列表（支持分页）"""
    storage = get_storage()

    # 使用 Storage 层的分页方法
    items, total = await storage.load_tokens_paginated(page, page_size, status)

    return TokenListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/tokens", dependencies=[Depends(verify_app_key)])
async def update_tokens(data: dict):
    """更新 Token 信息"""
    storage = get_storage()
    try:
        from app.services.token.models import TokenInfo

        async with storage.acquire_lock("tokens_save", timeout=10):
            existing = await storage.load_tokens() or {}
            normalized = {}
            allowed_fields = set(TokenInfo.model_fields.keys())
            existing_map = {}
            for pool_name, tokens in existing.items():
                if not isinstance(tokens, list):
                    continue
                pool_map = {}
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue
                    raw_token = token_data.get("token")
                    if isinstance(raw_token, str) and raw_token.startswith("sso="):
                        token_data["token"] = raw_token[4:]
                    token_key = token_data.get("token")
                    if isinstance(token_key, str):
                        pool_map[token_key] = token_data
                existing_map[pool_name] = pool_map
            for pool_name, tokens in (data or {}).items():
                if not isinstance(tokens, list):
                    continue
                pool_list = []
                for item in tokens:
                    if isinstance(item, str):
                        token_data = {"token": item}
                    elif isinstance(item, dict):
                        token_data = dict(item)
                    else:
                        continue

                    raw_token = token_data.get("token")
                    if isinstance(raw_token, str) and raw_token.startswith("sso="):
                        token_data["token"] = raw_token[4:]

                    base = existing_map.get(pool_name, {}).get(
                        token_data.get("token"), {}
                    )
                    merged = dict(base)
                    merged.update(token_data)
                    if merged.get("tags") is None:
                        merged["tags"] = []

                    filtered = {k: v for k, v in merged.items() if k in allowed_fields}
                    try:
                        info = TokenInfo(**filtered)
                        pool_list.append(info.model_dump())
                    except Exception as e:
                        logger.warning(f"Skip invalid token in pool '{pool_name}': {e}")
                        continue
                normalized[pool_name] = pool_list

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()
        _invalidate_stats_cache()
        return {"status": "success", "message": "Token 已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/batch-delete", dependencies=[Depends(verify_app_key)])
async def batch_delete_tokens(data: dict):
    """批量删除 Token（支持 ids 和 filter 模式）"""
    storage = get_storage()
    try:
        ids = data.get("ids") or data.get("tokens") or []
        status_filter = data.get("status") or data.get("filter", {}).get("status")

        if not ids and not status_filter:
            raise HTTPException(status_code=400, detail="需要提供 ids 或 status 筛选条件")

        async with storage.acquire_lock("tokens_save", timeout=10):
            all_tokens = await storage.load_tokens() or {}

            # 构建要删除的 token 集合
            to_delete = set()
            if ids:
                to_delete = set(str(t).strip() for t in ids if str(t).strip())

            # 按 status 筛选删除
            if status_filter:
                for pool_name, tokens in all_tokens.items():
                    if not isinstance(tokens, list):
                        continue
                    for t in tokens:
                        if isinstance(t, dict) and t.get("status") == status_filter:
                            to_delete.add(t.get("token"))

            if not to_delete:
                return {"status": "success", "affected": 0, "message": "没有匹配的 Token"}

            # 执行删除
            affected = 0
            normalized = {}
            for pool_name, tokens in all_tokens.items():
                if not isinstance(tokens, list):
                    continue
                pool_list = []
                for t in tokens:
                    token_str = t if isinstance(t, str) else t.get("token")
                    if token_str in to_delete:
                        affected += 1
                        continue
                    if isinstance(t, dict):
                        pool_list.append(t)
                    else:
                        pool_list.append({"token": t})
                normalized[pool_name] = pool_list

            await storage.save_tokens(normalized)
            mgr = await get_token_manager()
            await mgr.reload()

        _invalidate_stats_cache()
        return {"status": "success", "affected": affected, "message": f"已删除 {affected} 个 Token"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Batch delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tokens/stats", dependencies=[Depends(verify_app_key)])
async def get_tokens_stats():
    """获取 Token 统计信息（带缓存）"""
    global _stats_cache, _stats_cache_time

    # 检查缓存是否有效
    current_time = time.time()
    if _stats_cache and (current_time - _stats_cache_time) < _stats_cache_ttl:
        return _stats_cache

    storage = get_storage()
    all_tokens = await storage.load_tokens() or {}

    stats = {
        "total": 0,
        "active": 0,
        "cooling": 0,
        "expired": 0,
        "disabled": 0,
        "nsfw": 0,
        "no_nsfw": 0,
        "total_quota": 0,
    }

    for pool_name, tokens in all_tokens.items():
        if not isinstance(tokens, list):
            continue
        for t in tokens:
            if isinstance(t, str):
                stats["total"] += 1
                stats["active"] += 1
                stats["no_nsfw"] += 1
            elif isinstance(t, dict):
                stats["total"] += 1
                status = t.get("status", "active")
                if status == "active":
                    stats["active"] += 1
                    stats["total_quota"] += t.get("quota", 0)
                elif status == "cooling":
                    stats["cooling"] += 1
                elif status == "disabled":
                    stats["disabled"] += 1
                else:
                    stats["expired"] += 1

                tags = t.get("tags", [])
                if tags and "nsfw" in tags:
                    stats["nsfw"] += 1
                else:
                    stats["no_nsfw"] += 1

    # 更新缓存
    _stats_cache = stats
    _stats_cache_time = current_time

    return stats


@router.post("/tokens/import/async", dependencies=[Depends(verify_app_key)])
async def import_tokens_async(data: dict):
    """异步导入 Token（分片处理）"""
    try:
        tokens = data.get("tokens", [])
        pool = data.get("pool", "ssoBasic")

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        # 去重
        unique_tokens = list(dict.fromkeys([str(t).strip() for t in tokens if str(t).strip()]))

        if not unique_tokens:
            raise HTTPException(status_code=400, detail="No valid tokens")

        # 创建任务
        task = create_task(len(unique_tokens))

        async def _run():
            try:
                storage = get_storage()
                mgr = await get_token_manager()

                # 分片大小
                batch_size = 100
                success_count = 0
                fail_count = 0

                async with storage.acquire_lock("tokens_save", timeout=30):
                    existing = await storage.load_tokens() or {}

                    # 获取已存在的 token
                    existing_tokens = set()
                    for pool_name, pool_tokens in existing.items():
                        for t in pool_tokens:
                            token_str = t if isinstance(t, str) else t.get("token")
                            if token_str:
                                existing_tokens.add(token_str)

                    # 准备导入的 token
                    if pool not in existing:
                        existing[pool] = []

                    # 分片处理
                    for i in range(0, len(unique_tokens), batch_size):
                        if task.cancelled:
                            break

                        batch = unique_tokens[i:i + batch_size]

                        for token_str in batch:
                            if token_str in existing_tokens:
                                fail_count += 1
                                task.record(False)
                                continue

                            # 添加新 token
                            from app.services.token.models import TokenInfo
                            default_quota = 140 if pool == "ssoSuper" else 80

                            token_info = TokenInfo(
                                token=token_str,
                                status="active",
                                quota=default_quota,
                                note="",
                                tags=[],
                            )

                            existing[pool].append(token_info.model_dump())
                            existing_tokens.add(token_str)
                            success_count += 1
                            task.record(True)

                        # 每批次保存一次
                        await storage.save_tokens(existing)
                        await asyncio.sleep(0.1)  # 避免过载

                    # 最终保存
                    await storage.save_tokens(existing)
                    await mgr.reload()

                _invalidate_stats_cache()

                if task.cancelled:
                    task.finish_cancelled()
                    return

                result = {
                    "status": "success",
                    "summary": {
                        "total": len(unique_tokens),
                        "success": success_count,
                        "fail": fail_count,
                    },
                }
                task.finish(result)

            except Exception as e:
                logger.error(f"Import failed: {e}")
                task.fail_task(str(e))
            finally:
                asyncio.create_task(expire_task(task.id, 300))

        asyncio.create_task(_run())

        return {
            "status": "success",
            "task_id": task.id,
            "total": len(unique_tokens),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Import async failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/refresh", dependencies=[Depends(verify_app_key)])
async def refresh_tokens(data: dict):
    """刷新 Token 状态"""
    try:
        mgr = await get_token_manager()
        tokens = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        unique_tokens = list(dict.fromkeys(tokens))

        raw_results = await UsageService.batch(
            unique_tokens,
            mgr,
        )

        results = {}
        for token, res in raw_results.items():
            if res.get("ok"):
                results[token] = res.get("data", False)
            else:
                results[token] = False

        response = {"status": "success", "results": results}
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/refresh/async", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_async(data: dict):
    """刷新 Token 状态（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()
    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    unique_tokens = list(dict.fromkeys(tokens))

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _on_item(item: str, res: dict):
                task.record(bool(res.get("ok")))

            raw_results = await UsageService.batch(
                unique_tokens,
                mgr,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results: dict[str, bool] = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                if res.get("ok") and res.get("data") is True:
                    ok_count += 1
                    results[token] = True
                else:
                    fail_count += 1
                    results[token] = False

            await mgr._save(force=True)

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            task.finish(result)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            import asyncio
            asyncio.create_task(expire_task(task.id, 300))

    import asyncio
    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }


@router.get("/batch/{task_id}/stream")
async def batch_stream(task_id: str, request: Request):
    app_key = get_app_key()
    if app_key:
        key = request.query_params.get("app_key")
        if key != app_key:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_stream():
        queue = task.attach()
        try:
            yield f"data: {orjson.dumps({'type': 'snapshot', **task.snapshot()}).decode()}\n\n"

            final = task.final_event()
            if final:
                yield f"data: {orjson.dumps(final).decode()}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    final = task.final_event()
                    if final:
                        yield f"data: {orjson.dumps(final).decode()}\n\n"
                        return
                    continue

                yield f"data: {orjson.dumps(event).decode()}\n\n"
                if event.get("type") in ("done", "error", "cancelled"):
                    return
        finally:
            task.detach(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/batch/{task_id}/cancel", dependencies=[Depends(verify_app_key)])
async def batch_cancel(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.cancel()
    return {"status": "success"}


@router.post("/tokens/nsfw/enable", dependencies=[Depends(verify_app_key)])
async def enable_nsfw(data: dict):
    """批量开启 NSFW (Unhinged) 模式"""
    try:
        mgr = await get_token_manager()

        tokens = []
        if isinstance(data.get("token"), str) and data["token"].strip():
            tokens.append(data["token"].strip())
        if isinstance(data.get("tokens"), list):
            tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

        if not tokens:
            for pool_name, pool in mgr.pools.items():
                for info in pool.list():
                    raw = (
                        info.token[4:] if info.token.startswith("sso=") else info.token
                    )
                    tokens.append(raw)

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens available")

        unique_tokens = list(dict.fromkeys(tokens))

        raw_results = await NSFWService.batch(
            unique_tokens,
            mgr,
        )

        results = {}
        ok_count = 0
        fail_count = 0

        for token, res in raw_results.items():
            masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
            if res.get("ok") and res.get("data", {}).get("success"):
                ok_count += 1
                results[masked] = res.get("data", {})
            else:
                fail_count += 1
                results[masked] = res.get("data") or {"error": res.get("error")}

        response = {
            "status": "success",
            "summary": {
                "total": len(unique_tokens),
                "ok": ok_count,
                "fail": fail_count,
            },
            "results": results,
        }

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Enable NSFW failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tokens/nsfw/enable/async", dependencies=[Depends(verify_app_key)])
async def enable_nsfw_async(data: dict):
    """批量开启 NSFW (Unhinged) 模式（异步批量 + SSE 进度）"""
    mgr = await get_token_manager()

    tokens = []
    if isinstance(data.get("token"), str) and data["token"].strip():
        tokens.append(data["token"].strip())
    if isinstance(data.get("tokens"), list):
        tokens.extend([str(t).strip() for t in data["tokens"] if str(t).strip()])

    if not tokens:
        for pool_name, pool in mgr.pools.items():
            for info in pool.list():
                raw = info.token[4:] if info.token.startswith("sso=") else info.token
                tokens.append(raw)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens available")

    unique_tokens = list(dict.fromkeys(tokens))

    task = create_task(len(unique_tokens))

    async def _run():
        try:

            async def _on_item(item: str, res: dict):
                ok = bool(res.get("ok") and res.get("data", {}).get("success"))
                task.record(ok)

            raw_results = await NSFWService.batch(
                unique_tokens,
                mgr,
                on_item=_on_item,
                should_cancel=lambda: task.cancelled,
            )

            if task.cancelled:
                task.finish_cancelled()
                return

            results = {}
            ok_count = 0
            fail_count = 0
            for token, res in raw_results.items():
                masked = f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token
                if res.get("ok") and res.get("data", {}).get("success"):
                    ok_count += 1
                    results[masked] = res.get("data", {})
                else:
                    fail_count += 1
                    results[masked] = res.get("data") or {"error": res.get("error")}

            await mgr._save(force=True)

            result = {
                "status": "success",
                "summary": {
                    "total": len(unique_tokens),
                    "ok": ok_count,
                    "fail": fail_count,
                },
                "results": results,
            }
            task.finish(result)
        except Exception as e:
            task.fail_task(str(e))
        finally:
            import asyncio
            asyncio.create_task(expire_task(task.id, 300))

    import asyncio
    asyncio.create_task(_run())

    return {
        "status": "success",
        "task_id": task.id,
        "total": len(unique_tokens),
    }
