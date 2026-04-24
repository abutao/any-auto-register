"""CPA 号池监控 API"""

import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from core.config_store import config_store
from services.cpa_manager import (
    get_cpa_maintenance_config,
    list_auth_files,
    maintain_cpa_credentials,
)

router = APIRouter(prefix="/cpa", tags=["cpa"])

# 维护日志（内存中保留最近 100 条）
_maintenance_log: list[dict] = []
_log_lock = threading.Lock()
MAX_LOG_ENTRIES = 100


def _add_log(action: str, detail: dict):
    with _log_lock:
        _maintenance_log.append({
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action,
            **detail,
        })
        if len(_maintenance_log) > MAX_LOG_ENTRIES:
            _maintenance_log.pop(0)


# 包装 maintain 函数，记录日志
_original_maintain = maintain_cpa_credentials


def _maintain_with_log():
    result = _original_maintain()
    if result.get("ok"):
        _add_log("maintain", {
            "remaining": result.get("remaining", 0),
            "threshold": result.get("threshold", 0),
            "total": result.get("total", 0),
            "register": result.get("register", {}),
        })
    return result


@router.get("/status")
def get_cpa_status():
    """获取 CPA 号池实时状态"""
    config = get_cpa_maintenance_config()
    api_url = config_store.get("cpa_api_url", "").strip()

    if not api_url:
        return {
            "configured": False,
            "message": "CPA API URL 未配置",
        }

    # 查询号池
    try:
        files = list_auth_files()
    except Exception as e:
        return {
            "configured": True,
            "api_url": api_url,
            "error": f"查询失败: {e}",
        }

    total = len(files)
    active = sum(1 for f in files if f.get("status") not in ("error", "disabled") and not f.get("disabled"))
    error = sum(1 for f in files if f.get("status") == "error")
    disabled = sum(1 for f in files if f.get("disabled") or f.get("status") == "disabled")
    unavailable = sum(1 for f in files if f.get("unavailable"))

    # 按 provider 分类
    providers = {}
    for f in files:
        p = f.get("type") or f.get("provider") or "unknown"
        providers[p] = providers.get(p, 0) + 1

    return {
        "configured": True,
        "api_url": api_url,
        "enabled": config.enabled,
        "interval_minutes": config.interval_minutes,
        "threshold": config.threshold,
        "pool": {
            "total": total,
            "active": active,
            "error": error,
            "disabled": disabled,
            "unavailable": unavailable,
        },
        "providers": providers,
        "files": [
            {
                "name": f.get("name", ""),
                "email": f.get("email", ""),
                "status": f.get("status", ""),
                "disabled": f.get("disabled", False),
                "unavailable": f.get("unavailable", False),
                "created_at": f.get("created_at", ""),
                "updated_at": f.get("updated_at", f.get("modtime", "")),
                "last_refresh": f.get("last_refresh", ""),
            }
            for f in files[:200]  # 最多返回 200 条
        ],
    }


@router.get("/logs")
def get_cpa_logs():
    """获取 CPA 维护日志"""
    with _log_lock:
        return {"logs": list(reversed(_maintenance_log))}


@router.post("/maintain")
def trigger_maintain():
    """手动触发一次号池维护"""
    try:
        result = _maintain_with_log()
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}
