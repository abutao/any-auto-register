"""ChatGPT 支付 API — 手动支付 + 定时批量支付"""

import json
import os
import threading
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.db import AccountModel, engine

router = APIRouter(prefix="/payment", tags=["payment"])


# ── 请求模型 ──────────────────────────────────────────────────

class PaymentRequest(BaseModel):
    account_ids: list[int]
    plan: str = "plus"             # plus / business
    country: str = "US"            # 地址国家
    checkout_country: str = "AUTO" # 结账国家
    proxy: str = ""
    max_retries: int = 5
    headless: bool = True
    card_bin: str = ""


class AutoBatchRequest(BaseModel):
    plan: str = "plus"
    country: str = "US"
    checkout_country: str = "AUTO"
    proxy: str = ""
    batch_size: int = 10
    interval_minutes: int = 10
    max_batches: int = 0           # 0 = 无限
    max_retries: int = 5
    headless: bool = True
    card_bin: str = ""


# ── 支付任务状态 ──────────────────────────────────────────────

_payment_jobs: dict = {}
_payment_lock = threading.Lock()

_auto_batch_state = {
    "running": False,
    "batch_num": 0,
    "total_success": 0,
    "total_failed": 0,
    "message": "",
    "thread": None,
    "config": {},
}
_auto_batch_lock = threading.Lock()


def _get_proxy(proxy: str) -> str:
    if proxy:
        return proxy
    try:
        from core.config_store import config_store
        return config_store.get("default_proxy", "") or "http://127.0.0.1:7890"
    except Exception:
        return "http://127.0.0.1:7890"


def _get_account_cookies(acc: AccountModel) -> dict:
    """从账号记录中提取 cookies"""
    extra = acc.get_extra()
    cookies_raw = extra.get("cookies", "")
    if isinstance(cookies_raw, str) and cookies_raw:
        try:
            return json.loads(cookies_raw)
        except Exception:
            pass
    if isinstance(cookies_raw, dict):
        return cookies_raw

    # 从 cookie_file 读取
    cookie_file = extra.get("cookie_file", "")
    if cookie_file and os.path.isfile(cookie_file):
        try:
            with open(cookie_file, "r") as f:
                data = json.load(f)
                return data.get("cookies", data)
        except Exception:
            pass

    # 用 session_token 构造
    st = extra.get("session_token", "")
    if st:
        return {"__Secure-next-auth.session-token": st}

    return {}


def _update_account_plan(account_id: int, plan_type: str, status: str, extra_patch: dict = None):
    """更新账号的 plan_type 和状态"""
    from datetime import datetime, timezone
    with Session(engine) as session:
        acc = session.get(AccountModel, account_id)
        if acc:
            extra = acc.get_extra()
            extra["plan_type"] = plan_type
            extra["payment_status"] = status
            if extra_patch:
                extra.update(extra_patch)
            acc.set_extra(extra)
            acc.status = plan_type if status == "success" else acc.status
            acc.updated_at = datetime.now(timezone.utc)
            session.add(acc)
            session.commit()


def _run_single_payment(account_id: int, cookies: dict, email: str, config: dict) -> dict:
    """执行单个账号的支付"""
    from platforms.chatgpt.payment.payment_browser import do_payment

    plan = config.get("plan", "plus")
    country = config.get("country", "US")
    checkout_country = config.get("checkout_country", "AUTO")
    proxy = _get_proxy(config.get("proxy", ""))
    max_retries = config.get("max_retries", 5)
    headless = config.get("headless", True)

    # 标记为支付中
    _update_account_plan(account_id, "free", "processing")

    try:
        result = do_payment(
            cookies=cookies,
            email=email,
            plan_type=plan,
            country=country,
            checkout_country=checkout_country,
            max_card_retries=max_retries,
            headless=headless,
            proxy_url=proxy,
            use_proxy=bool(proxy),
        )

        if result.get("success"):
            _update_account_plan(account_id, plan, "success", {
                "payment_card_info": result.get("card_info", {}),
                "payment_country": country,
                "payment_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            return {"ok": True, "email": email, "plan": plan}
        else:
            _update_account_plan(account_id, "free", "failed", {
                "payment_error": result.get("message", ""),
            })
            return {"ok": False, "email": email, "error": result.get("message", "支付失败")}

    except Exception as e:
        _update_account_plan(account_id, "free", "failed", {
            "payment_error": str(e),
        })
        return {"ok": False, "email": email, "error": str(e)}


def _payment_worker(job_id: str, accounts: list, config: dict):
    """支付任务工作线程"""
    results = []
    for acc_info in accounts:
        with _payment_lock:
            job = _payment_jobs.get(job_id)
            if job and job.get("stopped"):
                break

        result = _run_single_payment(
            acc_info["id"], acc_info["cookies"], acc_info["email"], config
        )
        results.append(result)

        with _payment_lock:
            job = _payment_jobs.get(job_id)
            if job:
                if result["ok"]:
                    job["success"] += 1
                else:
                    job["failed"] += 1
                job["progress"] = f"{job['success'] + job['failed']}/{job['total']}"
                job["results"].append(result)

    with _payment_lock:
        job = _payment_jobs.get(job_id)
        if job:
            job["status"] = "done"
            job["message"] = f"完成: 成功 {job['success']}, 失败 {job['failed']}"


def _auto_batch_worker(config: dict):
    """定时批量支付工作线程"""
    batch_size = config.get("batch_size", 10)
    interval = config.get("interval_minutes", 10) * 60
    max_batches = config.get("max_batches", 0)
    plan = config.get("plan", "plus")

    batch_num = 0
    while True:
        with _auto_batch_lock:
            if not _auto_batch_state["running"]:
                break

        batch_num += 1
        if max_batches > 0 and batch_num > max_batches:
            break

        with _auto_batch_lock:
            _auto_batch_state["batch_num"] = batch_num
            _auto_batch_state["message"] = f"第 {batch_num} 批进行中..."

        # 取 free 账号
        with Session(engine) as session:
            q = select(AccountModel).where(
                AccountModel.platform == "chatgpt",
            ).order_by(AccountModel.id).limit(batch_size)
            all_accounts = session.exec(q).all()

            # 过滤出 free 账号
            candidates = []
            for acc in all_accounts:
                extra = acc.get_extra()
                pt = extra.get("plan_type", "free")
                ps = extra.get("payment_status", "")
                if pt == "free" and ps != "processing":
                    cookies = _get_account_cookies(acc)
                    if cookies:
                        candidates.append({
                            "id": acc.id,
                            "email": acc.email,
                            "cookies": cookies,
                        })

        if not candidates:
            with _auto_batch_lock:
                _auto_batch_state["message"] = "无可用 free 账号，等待下一轮..."
            time.sleep(interval)
            continue

        for acc_info in candidates:
            with _auto_batch_lock:
                if not _auto_batch_state["running"]:
                    break

            result = _run_single_payment(acc_info["id"], acc_info["cookies"], acc_info["email"], config)
            with _auto_batch_lock:
                if result.get("ok"):
                    _auto_batch_state["total_success"] += 1
                else:
                    _auto_batch_state["total_failed"] += 1
                _auto_batch_state["message"] = (
                    f"第 {batch_num} 批: 成功 {_auto_batch_state['total_success']}, "
                    f"失败 {_auto_batch_state['total_failed']}"
                )

        # 等待间隔
        wait_end = time.time() + interval
        while time.time() < wait_end:
            with _auto_batch_lock:
                if not _auto_batch_state["running"]:
                    break
            time.sleep(1)

    with _auto_batch_lock:
        _auto_batch_state["running"] = False
        _auto_batch_state["message"] = (
            f"已停止: 共 {_auto_batch_state['batch_num']} 批, "
            f"成功 {_auto_batch_state['total_success']}, "
            f"失败 {_auto_batch_state['total_failed']}"
        )


# ── API 路由 ──────────────────────────────────────────────────

@router.post("/start")
def start_payment(body: PaymentRequest):
    """对选中的账号发起支付"""
    if not body.account_ids:
        raise HTTPException(400, "未选择账号")

    accounts = []
    with Session(engine) as session:
        for aid in body.account_ids:
            acc = session.get(AccountModel, aid)
            if not acc or acc.platform != "chatgpt":
                continue
            cookies = _get_account_cookies(acc)
            if not cookies:
                continue
            accounts.append({"id": acc.id, "email": acc.email, "cookies": cookies})

    if not accounts:
        raise HTTPException(400, "没有可支付的账号（缺少 cookies）")

    job_id = f"pay_{int(time.time() * 1000)}"
    job = {
        "job_id": job_id,
        "status": "running",
        "total": len(accounts),
        "success": 0,
        "failed": 0,
        "progress": f"0/{len(accounts)}",
        "message": "支付中...",
        "stopped": False,
        "results": [],
    }

    with _payment_lock:
        _payment_jobs[job_id] = job

    config = body.model_dump()
    config["proxy"] = _get_proxy(body.proxy)

    t = threading.Thread(target=_payment_worker, args=(job_id, accounts, config), daemon=True)
    t.start()

    return {"ok": True, "job_id": job_id, "count": len(accounts)}


@router.get("/status/{job_id}")
def get_payment_status(job_id: str):
    """查询支付任务状态"""
    with _payment_lock:
        job = _payment_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        return {k: v for k, v in job.items() if k != "stopped"}


@router.post("/stop/{job_id}")
def stop_payment(job_id: str):
    """停止支付任务"""
    with _payment_lock:
        job = _payment_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        job["stopped"] = True
        job["message"] = "正在停止..."
    return {"ok": True}


@router.post("/auto-batch/start")
def start_auto_batch(body: AutoBatchRequest):
    """启动定时批量支付"""
    with _auto_batch_lock:
        if _auto_batch_state["running"]:
            return {"ok": False, "message": "定时支付已在运行中"}
        _auto_batch_state.update({
            "running": True,
            "batch_num": 0,
            "total_success": 0,
            "total_failed": 0,
            "message": "启动中...",
            "config": body.model_dump(),
        })

    config = body.model_dump()
    config["proxy"] = _get_proxy(body.proxy)
    t = threading.Thread(target=_auto_batch_worker, args=(config,), daemon=True)
    with _auto_batch_lock:
        _auto_batch_state["thread"] = t
    t.start()
    return {"ok": True, "message": "定时支付已启动"}


@router.post("/auto-batch/stop")
def stop_auto_batch():
    """停止定时批量支付"""
    with _auto_batch_lock:
        _auto_batch_state["running"] = False
        _auto_batch_state["message"] = "正在停止..."
    return {"ok": True}


@router.get("/auto-batch/status")
def get_auto_batch_status():
    """查询定时批量支付状态"""
    with _auto_batch_lock:
        return {
            "running": _auto_batch_state["running"],
            "batch_num": _auto_batch_state["batch_num"],
            "total_success": _auto_batch_state["total_success"],
            "total_failed": _auto_batch_state["total_failed"],
            "message": _auto_batch_state["message"],
        }
