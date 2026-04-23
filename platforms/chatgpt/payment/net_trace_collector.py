#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
net_trace_collector.py
======================
基于 DrissionPage listen API (CDP Network) 的网络请求收集器。
用于支付流程中采集所有网络请求，以便对比成功/失败案例。

用法:
    collector = NetTraceCollector(page, email="xxx@outlook.com", thread_id="t1")
    collector.start()          # 开始监听
    ... 执行支付操作 ...
    collector.stop_and_save()  # 停止并保存到 JSON

对比工具:
    python compare_net_traces.py --success output/net_traces/xxx_success.json \
                                  --failed  output/net_traces/yyy_failed.json
"""

import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRACE_DIR = os.path.join(_BASE_DIR, "output", "net_traces")

# 需要抓取 response body 的域名关键字
# 其他域名只记录 URL / status / headers，不存 body（避免日志过大）
BODY_CAPTURE_KEYWORDS = (
    "stripe.com",
    "chatgpt.com/backend-api",
    "openai.com/backend-api",
    "chatgpt.com/checkout",
    "chatgpt.com/payments",
    "chatgpt.com/ces",
    "challenges",
    "sentinel",
    "arkose",
    "turnstile",
    "hcaptcha",
)

# 需要完整记录 request body 的域名关键字
REQUEST_BODY_KEYWORDS = (
    "stripe.com",
    "chatgpt.com/backend-api",
    "openai.com/backend-api",
    "chatgpt.com/checkout",
    "chatgpt.com/payments",
)

# 完全跳过的 URL（静态资源等无意义流量）
SKIP_URL_PATTERNS = (
    ".woff2", ".woff", ".ttf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".css",
    "data:image/",
    "google-analytics.com",
    "googletagmanager.com",
    "facebook.net",
    "doubleclick.net",
)


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _should_skip_url(url: str) -> bool:
    low = url.lower()
    return any(p in low for p in SKIP_URL_PATTERNS)


def _should_capture_body(url: str) -> bool:
    low = url.lower()
    return any(k in low for k in BODY_CAPTURE_KEYWORDS)


def _should_capture_request_body(url: str) -> bool:
    low = url.lower()
    return any(k in low for k in REQUEST_BODY_KEYWORDS)


def _safe_str(v: Any, max_len: int = 4000) -> str:
    if v is None:
        return ""
    s = str(v) if not isinstance(v, str) else v
    return s[:max_len] if len(s) > max_len else s


def _classify_domain(url: str) -> str:
    """将 URL 归类到可读的域名分组，方便后续对比。"""
    low = url.lower()
    if "m.stripe.com" in low:
        return "stripe_fingerprint"
    if "r.stripe.com" in low:
        return "stripe_radar"
    if "js.stripe.com" in low:
        return "stripe_js"
    if "api.stripe.com" in low:
        return "stripe_api"
    if "stripe.com" in low:
        return "stripe_other"
    if "chatgpt.com/backend-api/payments" in low or "chatgpt.com/checkout" in low:
        return "openai_payment"
    if "chatgpt.com/backend-api" in low:
        return "openai_api"
    if "chatgpt.com/ces" in low:
        return "openai_ces"
    if "auth0.openai.com" in low or "auth.openai.com" in low:
        return "openai_auth"
    if "chatgpt.com" in low or "openai.com" in low:
        return "openai_other"
    if "challenges" in low or "sentinel" in low or "arkose" in low:
        return "challenge"
    if "turnstile" in low or "hcaptcha" in low:
        return "captcha"
    return "other"


def _sanitize_headers(headers: Any) -> Dict[str, str]:
    """安全提取 headers dict，移除超长 cookie 值。"""
    if not headers:
        return {}
    result = {}
    for k, v in (headers.items() if isinstance(headers, dict) else []):
        key = str(k)
        val = str(v) if v else ""
        # cookie 值截断，避免日志过大
        if key.lower() in ("cookie", "set-cookie"):
            val = val[:200] + "..." if len(val) > 200 else val
        result[key] = val
    return result


class NetTraceCollector:
    """CDP 级别网络请求收集器，基于 DrissionPage page.listen API。"""

    def __init__(
        self,
        page,
        email: str = "",
        thread_id: str = "",
        trace_dir: str = DEFAULT_TRACE_DIR,
        capture_all_bodies: bool = False,
    ):
        """
        Args:
            page: DrissionPage 的 ChromiumPage 实例
            email: 当前支付账号邮箱（用于文件命名）
            thread_id: 线程 ID
            trace_dir: trace 文件输出目录
            capture_all_bodies: True 则抓取所有响应体（大量数据）
        """
        self.page = page
        self.email = email
        self.thread_id = thread_id
        self.trace_dir = trace_dir
        self.capture_all_bodies = capture_all_bodies

        self._entries: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._running = False
        self._listener_thread: Optional[threading.Thread] = None
        self._start_time: Optional[float] = None
        self._meta: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    #  公开方法
    # ------------------------------------------------------------------ #

    def start(self, card_info: Optional[Dict] = None, extra_meta: Optional[Dict] = None) -> None:
        """开始监听网络请求。在 cookie 注入后、导航支付页面前调用。"""
        if self._running:
            return

        self._start_time = time.time()
        self._meta = {
            "email": self.email,
            "thread_id": self.thread_id,
            "start_time": _now_iso(),
            "card_info": self._safe_card_info(card_info),
            **(extra_meta or {}),
        }

        # 用 DrissionPage 的 listen API 启动 CDP Network 监听
        # targets=True 表示捕获所有 URL
        # method=True 表示所有 HTTP 方法
        # res_type=True 表示所有资源类型
        self.page.listen.set_targets(targets=True, is_regex=False, method=True, res_type=True)
        self.page.listen.start()

        self._running = True
        self._listener_thread = threading.Thread(
            target=self._consume_packets, daemon=True, name=f"net-trace-{self.thread_id}"
        )
        self._listener_thread.start()

    def update_card_info(self, card_info: Dict) -> None:
        """换卡时更新当前卡信息（方便日志中标记换卡时间点）。"""
        with self._lock:
            self._entries.append({
                "type": "card_change",
                "timestamp": _now_iso(),
                "elapsed_ms": int((time.time() - (self._start_time or time.time())) * 1000),
                "card_info": self._safe_card_info(card_info),
            })

    def mark_event(self, event_name: str, detail: Any = None) -> None:
        """手动插入一个自定义事件标记，如 'fill_start', 'submit_click' 等。"""
        with self._lock:
            self._entries.append({
                "type": "event",
                "event": event_name,
                "detail": _safe_str(detail, 2000) if detail else None,
                "timestamp": _now_iso(),
                "elapsed_ms": int((time.time() - (self._start_time or time.time())) * 1000),
            })

    def stop_and_save(self, success: bool = False, result_detail: str = "") -> str:
        """停止监听并保存 trace 到 JSON 文件。返回文件路径。"""
        self._running = False

        # 等待消费线程退出
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=3)

        # 停止 DrissionPage 监听
        try:
            self.page.listen.stop()
        except Exception:
            pass

        self._meta["end_time"] = _now_iso()
        self._meta["success"] = success
        self._meta["result_detail"] = result_detail
        self._meta["total_requests"] = len([e for e in self._entries if e.get("type") == "request"])
        self._meta["duration_seconds"] = round(time.time() - (self._start_time or time.time()), 1)

        # 生成摘要统计
        self._meta["summary"] = self._build_summary()

        trace_data = {
            "meta": self._meta,
            "entries": self._entries,
        }

        # 写入文件
        os.makedirs(self.trace_dir, exist_ok=True)
        status_tag = "success" if success else "failed"
        email_tag = re.sub(r'[^a-zA-Z0-9._@-]', '_', self.email or "unknown")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{email_tag}_{status_tag}_{ts}.json"
        filepath = os.path.join(self.trace_dir, filename)

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(trace_data, f, ensure_ascii=False, indent=2, default=str)
        except Exception as exc:
            print(f"[NetTrace] 保存 trace 失败: {exc}", flush=True)
            filepath = ""

        return filepath

    # ------------------------------------------------------------------ #
    #  内部方法
    # ------------------------------------------------------------------ #

    def _consume_packets(self) -> None:
        """后台线程：持续从 DrissionPage listener 队列中取数据包。"""
        try:
            for packet in self.page.listen.steps(timeout=2):
                if not self._running:
                    break
                if packet is False:
                    # timeout 返回 False，继续等
                    if not self._running:
                        break
                    continue
                try:
                    self._process_packet(packet)
                except Exception:
                    traceback.print_exc()
        except Exception:
            # listener 被 stop 后会抛异常，静默退出
            pass

    def _process_packet(self, packet) -> None:
        """将一个 DataPacket 转换为字典并存入 _entries。"""
        url = packet.url or ""
        if _should_skip_url(url):
            return

        method = packet.method or "GET"
        domain_class = _classify_domain(url)
        elapsed_ms = int((time.time() - (self._start_time or time.time())) * 1000)

        entry: Dict[str, Any] = {
            "type": "request",
            "timestamp": _now_iso(),
            "elapsed_ms": elapsed_ms,
            "method": method,
            "url": url,
            "domain_class": domain_class,
            "resource_type": packet.resourceType or "",
        }

        # --- 请求信息 ---
        try:
            req = packet.request
            if req:
                entry["request_headers"] = _sanitize_headers(req.headers)
                if _should_capture_request_body(url) or self.capture_all_bodies:
                    post_data = req.postData
                    if post_data:
                        entry["request_body"] = _safe_str(post_data, 8000)
        except Exception:
            pass

        # --- 响应信息 ---
        try:
            resp = packet.response
            if resp:
                entry["response_headers"] = _sanitize_headers(resp.headers)
                status_code = None
                try:
                    raw_resp = packet._raw_response
                    if isinstance(raw_resp, dict):
                        status_code = raw_resp.get("status")
                except Exception:
                    pass
                entry["status_code"] = status_code

                # 响应体：只对关键域名抓取
                if self.capture_all_bodies or _should_capture_body(url):
                    try:
                        body = resp.body
                        if isinstance(body, bytes):
                            entry["response_body"] = f"<binary {len(body)} bytes>"
                        elif body is not None:
                            entry["response_body"] = _safe_str(
                                json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else body,
                                8000,
                            )
                    except Exception:
                        entry["response_body"] = "<decode_error>"
        except Exception:
            pass

        # --- 失败信息 ---
        try:
            fail = packet.fail_info
            if fail:
                entry["fail_info"] = str(fail)
        except Exception:
            pass

        with self._lock:
            self._entries.append(entry)

    def _build_summary(self) -> Dict[str, Any]:
        """生成按域名分组的请求统计摘要。"""
        domain_stats: Dict[str, Dict[str, int]] = {}
        error_entries: List[Dict[str, str]] = []

        for e in self._entries:
            if e.get("type") != "request":
                continue
            dc = e.get("domain_class", "other")
            if dc not in domain_stats:
                domain_stats[dc] = {"total": 0, "success": 0, "failed": 0}
            domain_stats[dc]["total"] += 1

            status = e.get("status_code")
            if status and 200 <= int(status) < 400:
                domain_stats[dc]["success"] += 1
            elif status and int(status) >= 400:
                domain_stats[dc]["failed"] += 1
                error_entries.append({
                    "url": e.get("url", ""),
                    "status": status,
                    "response_body": (e.get("response_body") or "")[:500],
                })
            # 没有 status_code 的（pending/cancelled）不计入

        return {
            "domain_stats": domain_stats,
            "error_requests": error_entries[:20],  # 最多保留 20 条错误
        }

    @staticmethod
    def _safe_card_info(card_info: Optional[Dict]) -> Optional[Dict[str, str]]:
        if not card_info:
            return None
        safe = {}
        for k in ("card_number_formatted", "expiry_date", "cvv", "bin_prefix",
                   "full_name", "country", "state", "city", "address",
                   "address_line2", "zip_code"):
            v = card_info.get(k)
            if v is not None:
                safe[k] = str(v)
        return safe
