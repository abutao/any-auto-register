#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ChatGPT 支付脚本 - 优化版（API 直接调用）

功能：
- 使用 DrissionPage 自动化 Chrome
- 通过直接调用后端 API 绕过 A/B 测试
- 强制使用 OpenAI 内部支付界面
- 确保促销折扣正确应用（首月 $0）
- 自动填充支付表单
- 支持 Plus 和 Business 套餐

用法:
    python chatgpt_payment_optimized.py --input result.json [--plan plus|business]

返回 JSON:
    {"success": true, "message": "支付成功", "card_info": {...}}

依赖:
    pip install DrissionPage faker requests
"""

import os
import sys
import time
import json
import random
import re
import argparse
import signal
import subprocess
import requests
import platform
import sqlite3
from datetime import datetime
from functools import lru_cache
from faker import Faker
from DrissionPage import ChromiumPage, ChromiumOptions

try:
    from platforms.chatgpt.payment.net_trace_collector import NetTraceCollector
except ImportError:
    try:
        from platforms.chatgpt.payment.net_trace_collector import NetTraceCollector
    except ImportError:
        NetTraceCollector = None  # type: ignore

try:
    from platforms.chatgpt.payment.hcaptcha_solver import detect_and_solve_hcaptcha as _solve_hcaptcha
    from platforms.chatgpt.payment.hcaptcha_solver import detect_hcaptcha as _detect_hcaptcha
except ImportError:
    try:
        from platforms.chatgpt.payment.hcaptcha_solver import detect_and_solve_hcaptcha as _solve_hcaptcha
        from platforms.chatgpt.payment.hcaptcha_solver import detect_hcaptcha as _detect_hcaptcha
    except ImportError:
        _solve_hcaptcha = None  # type: ignore
        _detect_hcaptcha = None  # type: ignore

# ==================== 配置 ====================
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    # 项目根目录: payment_browser.py → payment/ → chatgpt/ → platforms/ → 项目根
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890
RESULTS_DIR = os.path.join(_BASE_DIR, "Results_ChatGPT")

# 共享信号文件目录
SIGNAL_DIR = os.path.join(_BASE_DIR, ".signals")
os.makedirs(SIGNAL_DIR, exist_ok=True)

# DEBUG 模式
DEBUG_MODE = True


# ==================== 日志 ====================
class Logger:
    def __init__(self, quiet=False):
        self.quiet = quiet

    def log(self, step: str, msg: str, level: str = "INFO"):
        if self.quiet and level == "INFO":
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = {"INFO": "ℹ️", "SUCCESS": "✓", "ERROR": "✗", "WARN": "⚠"}.get(level, "")
        print(f"[{ts}] [{step}] {prefix} {msg}", file=sys.stderr)


logger = Logger()

SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
CHECKOUT_COOKIE_CLEANUP_NAMES = (
    "__Secure-next-auth.callback-url",
    "oai-chat-web-route",
    "oai-client-auth-info",
    "__cflb",
    "_dd_s",
    "g_state",
    "oai-gn",
)
PAYPAL_PROFILE_LOCK_NAME = ".paypal_profile.lock"


def _prepare_payment_cookies(raw_cookies, session_token: str = "", compat_session_token: str = "") -> dict[str, str]:
    items: list[tuple[str, str]] = []
    if isinstance(raw_cookies, dict):
        iterable = [{"name": name, "value": value} for name, value in raw_cookies.items()]
    elif isinstance(raw_cookies, list):
        iterable = raw_cookies
    else:
        iterable = []

    for item in iterable:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        value = str(item.get("value", "") or "")
        if not name or not value or name.startswith("__cf"):
            continue
        items.append((name, value))

    cookie_map: dict[str, str] = {}
    order: list[str] = []
    first_session_index: int | None = None
    for name, value in items:
        if name not in cookie_map:
            order.append(name)
            if first_session_index is None and (
                name == SESSION_COOKIE_NAME or name.startswith(f"{SESSION_COOKIE_NAME}.")
            ):
                first_session_index = len(order) - 1
        cookie_map[name] = value

    chunk_parts: list[str] = []
    has_chunk = False
    for idx in range(16):
        chunk_value = str(cookie_map.get(f"{SESSION_COOKIE_NAME}.{idx}") or "").strip()
        if chunk_value:
            chunk_parts.append(chunk_value)
            has_chunk = True
            continue
        if has_chunk:
            break

    exact_session = str(cookie_map.get(SESSION_COOKIE_NAME) or "").strip()
    merged_session = "".join(chunk_parts).strip()
    fallback_session = str(session_token or "").strip()
    compat_fallback_session = str(compat_session_token or "").strip()

    resolved_session = ""
    for candidate in (merged_session, exact_session, fallback_session, compat_fallback_session):
        if candidate and len(candidate) >= len(resolved_session):
            resolved_session = candidate

    normalized: dict[str, str] = {}
    inserted_session = False
    for idx, name in enumerate(order):
        if name == SESSION_COOKIE_NAME or name.startswith(f"{SESSION_COOKIE_NAME}."):
            if (
                not inserted_session
                and resolved_session
                and first_session_index is not None
                and idx >= first_session_index
            ):
                normalized[SESSION_COOKIE_NAME] = resolved_session
                inserted_session = True
            continue
        normalized[name] = cookie_map[name]

    if resolved_session and not inserted_session:
        normalized[SESSION_COOKIE_NAME] = resolved_session

    return normalized


def _extract_payment_cookies_from_payload(payload) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    return _prepare_payment_cookies(
        payload.get("cookies") or payload.get("payment_cookies") or {},
        session_token=str(payload.get("session_token") or ""),
        compat_session_token=str(payload.get("compat_session_token") or ""),
    )


def _build_browser_cookie_payloads(cookies, domain: str = "chatgpt.com") -> list[dict]:
    clean_domain = str(domain or "chatgpt.com").strip() or "chatgpt.com"
    normalized_cookies = _prepare_payment_cookies(cookies)
    payloads: list[dict] = []
    for name, value in normalized_cookies.items():
        if not name or value is None or str(value) == "" or name.startswith("__cf"):
            continue
        value = str(value)
        if name.startswith("__Host-"):
            payloads.append({
                "name": name,
                "value": value,
                "url": f"https://{clean_domain}/",
                "path": "/",
                "secure": True,
            })
            continue

        item = {
            "name": name,
            "value": value,
            "domain": f".{clean_domain.lstrip('.')}",
            "path": "/",
        }
        if name.startswith("__Secure-"):
            item["secure"] = True
        payloads.append(item)
    return payloads


def _get_checkout_cookie_cleanup_targets(domain: str = "chatgpt.com") -> list[dict]:
    clean_domain = str(domain or "chatgpt.com").strip() or "chatgpt.com"
    return [{"domain": clean_domain, "path": "/", "name": name} for name in CHECKOUT_COOKIE_CLEANUP_NAMES]


def _prune_checkout_request_cookies(page, domain: str = "chatgpt.com") -> int:
    removed = 0
    for item in _get_checkout_cookie_cleanup_targets(domain):
        try:
            page._run_cdp_loaded("Network.deleteCookies", **item)
            removed += 1
        except Exception:
            pass
    return removed


def _is_checkout_error_page(page_text: str = "", title: str = "", html: str = "") -> bool:
    haystack = "\n".join(
        str(part or "").strip().lower()
        for part in (title, page_text, html)
        if str(part or "").strip()
    )
    if not haystack:
        return False
    error_markers = (
        "http error 431",
        "request header fields too large",
        "该网页无法正常运作",
        "如果问题仍然存在，请与网站所有者联系",
        "重新加载",
    )
    return any(marker in haystack for marker in error_markers)


def _read_page_text(page, limit: int = 2000) -> tuple[str, str]:
    title = ""
    body_text = ""
    try:
        title = str(page.title or "")
    except Exception:
        title = ""
    try:
        body_text = str(
            page.run_js(
                f"return document.body ? document.body.innerText.slice(0, {max(100, int(limit))}) : ''"
            ) or ""
        )
    except Exception:
        body_text = ""
    return title, body_text


def _validate_checkout_page(page, expected_url: str = "") -> dict:
    current_url = ""
    try:
        current_url = str(page.url or "")
    except Exception:
        current_url = ""

    title, body_text = _read_page_text(page)
    if _is_checkout_error_page(body_text, title=title):
        return {
            "success": False,
            "message": "checkout 页面返回 HTTP 431",
            "current_url": current_url,
            "title": title,
            "body_text": body_text,
        }

    if "checkout" in current_url or "payment" in current_url:
        return {
            "success": True,
            "payment_url": expected_url or current_url,
            "current_url": current_url,
            "title": title,
            "body_text": body_text,
        }

    return {
        "success": True,
        "payment_url": expected_url or current_url,
        "current_url": current_url,
        "title": title,
        "body_text": body_text,
        "message": f"导航后 URL 异常: {current_url}",
    }


def _is_page_refresh_retryable_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    retry_markers = (
        "the page is refreshed",
        "please wait until the page is refreshed or loaded",
        "context lost",
        "execution context was destroyed",
    )
    return any(marker in text for marker in retry_markers)


def _wait_for_page_ready(page, timeout: float = 12.0, poll_interval: float = 0.5) -> bool:
    deadline = time.time() + max(1.0, float(timeout or 0))
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            state = page.run_js(
                """
                return {
                    href: location.href,
                    readyState: document.readyState,
                    hasBootstrap: !!document.getElementById('client-bootstrap'),
                };
                """
            ) or {}
            ready_state = str(state.get("readyState") or "")
            if ready_state == "complete":
                return True
        except Exception as exc:
            last_exc = exc
        time.sleep(max(0.1, float(poll_interval or 0.5)))
    if last_exc:
        raise last_exc
    return False


# ==================== 信号机制 ====================
def normalize_card_bin(card_bin: str) -> str:
    clean = ''.join(ch for ch in str(card_bin or '').strip() if ch.isdigit())
    if not clean:
        return ''
    if len(clean) < 4 or len(clean) > 8:
        raise ValueError('卡头必须为 4-8 位数字')
    return clean


def write_signal(action: str, card_bin: str = ''):
    """写入全局信号"""
    signal_file = os.path.join(SIGNAL_DIR, "global_action.signal")
    clean_card_bin = normalize_card_bin(card_bin)
    action_payload = f"{action}|{clean_card_bin}" if clean_card_bin else action
    with open(signal_file, 'w') as f:
        f.write(f"{action_payload}:{time.time()}")
    logger.log("Signal", f"已发送全局信号: {action}{'|' + clean_card_bin if clean_card_bin else ''}", "SUCCESS")


def read_signal() -> tuple:
    """读取全局信号"""
    signal_file = os.path.join(SIGNAL_DIR, "global_action.signal")
    try:
        if os.path.exists(signal_file):
            with open(signal_file, 'r') as f:
                content = f.read().strip()
            if ':' in content:
                payload, ts = content.rsplit(':', 1)
                if '|' in payload:
                    action, card_bin = payload.split('|', 1)
                else:
                    action, card_bin = payload, ''
                return action, float(ts), normalize_card_bin(card_bin)
    except:
        pass
    return None, 0, ''


def clear_signal():
    """清除信号"""
    signal_file = os.path.join(SIGNAL_DIR, "global_action.signal")
    try:
        if os.path.exists(signal_file):
            os.remove(signal_file)
    except:
        pass


def trigger_all_browsers(action: str, card_bin: str = ''):
    """触发所有浏览器执行指定操作"""
    valid_actions = ['fill', 'submit', 'new_card', 'replace_card_only', 'replace_billing_only', 'close_connections', 'check_ip']
    if action not in valid_actions:
        print(json.dumps({"success": False, "error": f"无效操作: {action}, 可选: {valid_actions}"}))
        return

    try:
        clean_card_bin = normalize_card_bin(card_bin) if action in {'new_card', 'replace_card_only'} else ''
    except ValueError as exc:
        print(json.dumps({"success": False, "message": str(exc)}))
        return

    write_signal(action, clean_card_bin)
    print(json.dumps({"success": True, "message": f"已触发所有浏览器执行: {action}"}))


# ==================== 卡片生成 ====================
class CardGenerator:
    @staticmethod
    def luhn_check(partial):
        total, alt = 0, True
        for i in range(len(partial) - 1, -1, -1):
            d = int(partial[i])
            if alt:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
            alt = not alt
        return str((10 - (total % 10)) % 10)

    @classmethod
    def generate(cls, bin_prefix="625003"):
        clean = ''.join(filter(str.isdigit, str(bin_prefix)))
        for _ in range(50):
            acc = ''.join(str(random.randint(0, 9)) for _ in range(16 - len(clean) - 1))
            partial = clean + acc
            check = cls.luhn_check(partial)
            card = partial + check
            if len(card) == 16:
                break
        em = f"{random.choice([3, 6, 9, 12]):02d}"
        ey = str(datetime.now().year + random.randint(3, 5))[-2:]
        return {
            'card_number': card,
            'card_number_formatted': ' '.join(card[i:i + 4] for i in range(0, 16, 4)),
            'expiry_date': f"{em}/{ey}",
            'cvv': str(random.randint(100, 999)),
            'bin_prefix': bin_prefix
        }


def _format_card_number(card_number: str) -> str:
    raw = ''.join(ch for ch in str(card_number or "") if ch.isdigit())
    if not raw:
        return ""
    return " ".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def fetch_us_address(state='delaware') -> dict:
    """获取美国地址"""
    try:
        r = requests.post('https://www.meiguodizhi.com/api/v1/dz',
                          headers={'Content-Type': 'application/json'},
                          json={'path': f'/usa-address/{state}', 'method': 'address'}, timeout=5)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'ok' and d.get('address'):
                a = d['address']
                return {'full_name': a.get('Full_Name', ''), 'address': a.get('Address', ''),
                        'city': a.get('City', ''), 'state': a.get('State', ''), 'zip_code': a.get('Zip_Code', '')}
    except:
        pass
    fake = Faker('en_US')
    sa = fake.state_abbr()
    return {'full_name': fake.name(), 'address': fake.street_address(),
            'city': fake.city(), 'state': sa, 'zip_code': fake.zipcode_in_state(sa)}


BILLING_COUNTRY_LABELS = {
    "SG": "新加坡",
    "KR": "韩国",
    "US": "美国",
    "DE": "德国",
}

SUPPORTED_CHECKOUT_COUNTRIES = {
    "CN", "US", "CA", "GB", "AU", "JP", "KR", "SG", "AE", "HK", "TW", "DE", "FR", "FI", "IT",
}

KOREAN_BILLING_PROFILES_PATH = os.path.join(_BASE_DIR, "resources", "korean_billing_profiles.json")
SINGAPORE_BILLING_PROFILES_PATH = os.path.join(_BASE_DIR, "resources", "singapore_billing_profiles.json")

UNIT_ADDRESS_RE = re.compile(r"\d+동\s*\d+호|#\d+-?\d+|\d+호|(B|C)동\s*\d+호")

REAL_ADDRESS_DATABASE = {
    "US": [
        {"address": "350 Fifth Avenue", "city": "New York", "state": "NY", "zip_code": "10118", "address_line2": "Suite 7700"},
        {"address": "30 Hudson Yards", "city": "New York", "state": "NY", "zip_code": "10001", "address_line2": ""},
        {"address": "1515 Broadway", "city": "New York", "state": "NY", "zip_code": "10036", "address_line2": ""},
        {"address": "200 Park Avenue", "city": "New York", "state": "NY", "zip_code": "10166", "address_line2": "Suite 1700"},
        {"address": "1600 Amphitheatre Parkway", "city": "Mountain View", "state": "CA", "zip_code": "94043", "address_line2": ""},
        {"address": "1 Apple Park Way", "city": "Cupertino", "state": "CA", "zip_code": "95014", "address_line2": ""},
        {"address": "1355 Market Street", "city": "San Francisco", "state": "CA", "zip_code": "94103", "address_line2": "Suite 900"},
        {"address": "1601 Willow Road", "city": "Menlo Park", "state": "CA", "zip_code": "94025", "address_line2": ""},
        {"address": "2025 Gateway Place", "city": "San Jose", "state": "CA", "zip_code": "95110", "address_line2": "Suite 500"},
        {"address": "2550 N First Street", "city": "San Jose", "state": "CA", "zip_code": "95131", "address_line2": ""},
        {"address": "400 S El Camino Real", "city": "San Mateo", "state": "CA", "zip_code": "94402", "address_line2": "Suite 1100"},
        {"address": "3979 Freedom Circle", "city": "Santa Clara", "state": "CA", "zip_code": "95054", "address_line2": "Suite 300"},
        {"address": "410 Terry Ave N", "city": "Seattle", "state": "WA", "zip_code": "98109", "address_line2": ""},
        {"address": "1 Microsoft Way", "city": "Redmond", "state": "WA", "zip_code": "98052", "address_line2": ""},
        {"address": "500 108th Ave NE", "city": "Bellevue", "state": "WA", "zip_code": "98004", "address_line2": "Suite 800"},
        {"address": "701 Pike Street", "city": "Seattle", "state": "WA", "zip_code": "98101", "address_line2": "Suite 1850"},
        {"address": "233 S Wacker Drive", "city": "Chicago", "state": "IL", "zip_code": "60606", "address_line2": "Suite 4900"},
        {"address": "200 W Jackson Boulevard", "city": "Chicago", "state": "IL", "zip_code": "60606", "address_line2": "Suite 1500"},
        {"address": "100 Congress Ave", "city": "Austin", "state": "TX", "zip_code": "78701", "address_line2": "Suite 400"},
        {"address": "2200 Ross Avenue", "city": "Dallas", "state": "TX", "zip_code": "75201", "address_line2": "Suite 3600"},
        {"address": "1301 Fannin Street", "city": "Houston", "state": "TX", "zip_code": "77002", "address_line2": "Suite 2500"},
        {"address": "One Beacon Street", "city": "Boston", "state": "MA", "zip_code": "02108", "address_line2": "Suite 1500"},
        {"address": "1900 Reston Metro Plaza", "city": "Reston", "state": "VA", "zip_code": "20190", "address_line2": "Suite 600"},
        {"address": "750 N San Vicente Boulevard", "city": "West Hollywood", "state": "CA", "zip_code": "90069", "address_line2": "Suite 800"},
        {"address": "8000 Avalon Boulevard", "city": "Alpharetta", "state": "GA", "zip_code": "30009", "address_line2": "Suite 100"},
    ],
    "SG": [
        {"address": "1 Raffles Place", "city": "Singapore", "state": "", "zip_code": "048616", "address_line2": "#44-01"},
        {"address": "9 Raffles Place", "city": "Singapore", "state": "", "zip_code": "048619", "address_line2": "#58-01"},
        {"address": "50 Raffles Place", "city": "Singapore", "state": "", "zip_code": "048623", "address_line2": "#32-01"},
        {"address": "10 Marina Boulevard", "city": "Singapore", "state": "", "zip_code": "018983", "address_line2": "#32-01"},
        {"address": "12 Marina Boulevard", "city": "Singapore", "state": "", "zip_code": "018982", "address_line2": "#17-01"},
        {"address": "8 Marina View", "city": "Singapore", "state": "", "zip_code": "018960", "address_line2": "#37-01"},
        {"address": "9 Straits View", "city": "Singapore", "state": "", "zip_code": "018937", "address_line2": "#06-07"},
        {"address": "1 George Street", "city": "Singapore", "state": "", "zip_code": "049145", "address_line2": "#10-01"},
        {"address": "80 Robinson Road", "city": "Singapore", "state": "", "zip_code": "068898", "address_line2": "#20-01"},
        {"address": "77 Robinson Road", "city": "Singapore", "state": "", "zip_code": "068896", "address_line2": "#30-01"},
        {"address": "168 Robinson Road", "city": "Singapore", "state": "", "zip_code": "068912", "address_line2": "#12-01"},
        {"address": "6 Battery Road", "city": "Singapore", "state": "", "zip_code": "049909", "address_line2": "#23-01"},
        {"address": "3 Church Street", "city": "Singapore", "state": "", "zip_code": "049483", "address_line2": "#25-01"},
        {"address": "10 Collyer Quay", "city": "Singapore", "state": "", "zip_code": "049315", "address_line2": "#10-01"},
        {"address": "1 Temasek Avenue", "city": "Singapore", "state": "", "zip_code": "039192", "address_line2": "#36-01"},
        {"address": "391A Orchard Road", "city": "Singapore", "state": "", "zip_code": "238873", "address_line2": "#21-01"},
        {"address": "152 Beach Road", "city": "Singapore", "state": "", "zip_code": "189721", "address_line2": "#30-01"},
        {"address": "5 Shenton Way", "city": "Singapore", "state": "", "zip_code": "068808", "address_line2": "#33-01"},
        {"address": "8 Shenton Way", "city": "Singapore", "state": "", "zip_code": "068811", "address_line2": "#45-01"},
        {"address": "2 Shenton Way", "city": "Singapore", "state": "", "zip_code": "068804", "address_line2": "#04-01"},
        {"address": "63 Market Street", "city": "Singapore", "state": "", "zip_code": "048942", "address_line2": "#10-01"},
        {"address": "1 Harbourfront Walk", "city": "Singapore", "state": "", "zip_code": "098585", "address_line2": "#02-02"},
        {"address": "1 Kim Seng Promenade", "city": "Singapore", "state": "", "zip_code": "237994", "address_line2": "#12-01"},
        {"address": "71 Robinson Road", "city": "Singapore", "state": "", "zip_code": "068895", "address_line2": "#14-01"},
        {"address": "8 Cross Street", "city": "Singapore", "state": "", "zip_code": "048424", "address_line2": "#28-01"},
    ],
    "KR": [
        {"address": "311 Gangnam-daero", "city": "Seocho-gu", "state": "Seoul", "zip_code": "06628", "address_line2": ""},
        {"address": "152 Teheran-ro", "city": "Gangnam-gu", "state": "Seoul", "zip_code": "06236", "address_line2": ""},
        {"address": "110 Sejong-daero", "city": "Jung-gu", "state": "Seoul", "zip_code": "04524", "address_line2": ""},
        {"address": "55 Eulji-ro", "city": "Jung-gu", "state": "Seoul", "zip_code": "04535", "address_line2": ""},
        {"address": "400 World Cup buk-ro", "city": "Mapo-gu", "state": "Seoul", "zip_code": "03925", "address_line2": ""},
        {"address": "269 Olympic-ro", "city": "Songpa-gu", "state": "Seoul", "zip_code": "05510", "address_line2": ""},
        {"address": "58 Saemunan-ro", "city": "Jongno-gu", "state": "Seoul", "zip_code": "03186", "address_line2": ""},
        {"address": "416 Hangang-daero", "city": "Yongsan-gu", "state": "Seoul", "zip_code": "04323", "address_line2": ""},
        {"address": "330 Yeongdong-daero", "city": "Gangnam-gu", "state": "Seoul", "zip_code": "06162", "address_line2": ""},
        {"address": "33 Jong-ro", "city": "Jongno-gu", "state": "Seoul", "zip_code": "03149", "address_line2": ""},
        {"address": "343 Songpa-daero", "city": "Songpa-gu", "state": "Seoul", "zip_code": "05661", "address_line2": ""},
        {"address": "30 Sogong-ro", "city": "Jung-gu", "state": "Seoul", "zip_code": "04532", "address_line2": ""},
        {"address": "85 Namdaemun-ro", "city": "Jung-gu", "state": "Seoul", "zip_code": "04532", "address_line2": ""},
        {"address": "20 Gukjegeumyung-ro", "city": "Yeongdeungpo-gu", "state": "Seoul", "zip_code": "07326", "address_line2": ""},
        {"address": "92 Hangang-daero", "city": "Yongsan-gu", "state": "Seoul", "zip_code": "04386", "address_line2": ""},
        {"address": "125 Gasan digital 1-ro", "city": "Geumcheon-gu", "state": "Seoul", "zip_code": "08507", "address_line2": ""},
        {"address": "300 Olympic-ro", "city": "Songpa-gu", "state": "Seoul", "zip_code": "05551", "address_line2": ""},
        {"address": "780 Gyeongin-ro", "city": "Guro-gu", "state": "Seoul", "zip_code": "08212", "address_line2": ""},
        {"address": "50 Sajik-ro 8-gil", "city": "Jongno-gu", "state": "Seoul", "zip_code": "03170", "address_line2": ""},
        {"address": "194 Haeundae-haebyon-ro", "city": "Haeundae-gu", "state": "Busan", "zip_code": "48099", "address_line2": ""},
        {"address": "120 Heungdeok jungang-ro", "city": "Giheung-gu", "state": "Gyeonggi-do", "zip_code": "16950", "address_line2": ""},
        {"address": "11 Wonhyo-ro 90-gil", "city": "Yongsan-gu", "state": "Seoul", "zip_code": "04390", "address_line2": ""},
        {"address": "50 Jong-ro 1-gil", "city": "Jongno-gu", "state": "Seoul", "zip_code": "03142", "address_line2": ""},
        {"address": "86 Cheongpa-ro 47-gil", "city": "Yongsan-gu", "state": "Seoul", "zip_code": "04317", "address_line2": ""},
        {"address": "241 Gonghang-daero", "city": "Gangseo-gu", "state": "Seoul", "zip_code": "07803", "address_line2": ""},
    ],
}

PAYMENT_SUCCESS_RECORDS_DB_PATH = os.environ.get("PAYMENT_SUCCESS_RECORDS_DB_PATH") or os.path.join(_BASE_DIR, "output", "accounts.db")
KR_SUCCESS_PROFILE_DEFAULT_MODE = str(os.environ.get("KR_SUCCESS_PROFILE_MODE") or "split_priority")


def normalize_billing_country(country: str = "SG") -> str:
    clean = str(country or "SG").strip().upper() or "SG"
    return clean if clean in BILLING_COUNTRY_LABELS else "SG"


def normalize_plan_type(plan_type: str = "plus") -> str:
    clean = str(plan_type or "plus").strip().lower()
    return "business" if clean in {"business", "team"} else "plus"


def normalize_currency(currency: str = "USD") -> str:
    return str(currency or "USD").strip().upper() or "USD"


def normalize_checkout_country(country: str = "", fallback: str = "US") -> str:
    normalized_fallback = str(fallback or "US").strip().upper() or "US"
    if normalized_fallback not in SUPPORTED_CHECKOUT_COUNTRIES:
        normalized_fallback = "US"
    normalized_country = str(country or "").strip().upper()
    return normalized_country if normalized_country in SUPPORTED_CHECKOUT_COUNTRIES else normalized_fallback


def build_pricing_config_country_sequence(country: str = "", fallback: str = "US") -> list[str]:
    normalized_fallback = normalize_checkout_country(fallback, "US")
    normalized_target = normalize_checkout_country(country, normalized_fallback)
    return [normalized_fallback] if normalized_target == normalized_fallback else [normalized_fallback, normalized_target]


def resolve_checkout_country(checkout_country: str = "", address_country: str = "", fallback: str = "US") -> str:
    normalized_fallback = normalize_checkout_country(fallback, "US")
    normalized_checkout = str(checkout_country or "").strip().upper()
    if normalized_checkout and normalized_checkout != "AUTO":
        return normalize_checkout_country(normalized_checkout, normalized_fallback)
    normalized_address = str(address_country or "").strip().upper()
    if normalized_address and normalized_address != "AUTO":
        return normalize_checkout_country(normalized_address, normalized_fallback)
    return normalized_fallback


def get_default_checkout_currency(plan_type: str = "business", country: str = "US") -> str:
    if normalize_plan_type(plan_type) != "business":
        return "USD"
    normalized_country = normalize_checkout_country(country, "US")
    if normalized_country in {"DE", "FR", "FI", "IT"}:
        return "EUR"
    if normalized_country == "GB":
        return "GBP"
    return "USD"


def _read_nested_string(source, paths) -> str:
    for path in paths or []:
        value = source
        for segment in path:
            if value is None or not isinstance(value, dict):
                value = ""
                break
            value = value.get(segment)
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def derive_checkout_context_from_pricing_config(payload) -> dict:
    raw_country = _read_nested_string(
        payload,
        [
            ["billing_details", "country"],
            ["country_code"],
            ["countryCode"],
            ["country"],
            ["checkout", "billing_details", "country"],
            ["checkout", "country_code"],
        ],
    ).upper()
    country = raw_country if raw_country in SUPPORTED_CHECKOUT_COUNTRIES else ""
    currency = _read_nested_string(
        payload,
        [
            ["billing_details", "currency"],
            ["currency_code"],
            ["currencyCode"],
            ["currency"],
            ["checkout", "billing_details", "currency"],
            ["checkout", "currency_code"],
        ],
    ).upper()
    processor_entity = _read_nested_string(
        payload,
        [
            ["processor_entity"],
            ["processorEntity"],
            ["checkout", "processor_entity"],
            ["checkout", "processorEntity"],
            ["payment", "processor_entity"],
        ],
    )
    return {
        "country": country,
        "currency": currency,
        "processorEntity": processor_entity,
    }


def build_chatgpt_checkout_url(session_or_options, plan_type: str = "business") -> str:
    clean_url = ""
    if isinstance(session_or_options, dict):
        clean_url = str(session_or_options.get("url") or "").strip()
    if clean_url:
        return clean_url

    if normalize_plan_type(plan_type) not in {"business", "plus"}:
        raise ValueError(f"不支持的套餐类型: {plan_type}")

    if isinstance(session_or_options, dict):
        session_id = str(
            session_or_options.get("sessionId")
            or session_or_options.get("checkoutSessionId")
            or session_or_options.get("checkout_session_id")
            or session_or_options.get("session_id")
            or ""
        ).strip()
        processor_entity = str(
            session_or_options.get("processorEntity")
            or session_or_options.get("processor_entity")
            or ""
        ).strip()
    else:
        session_id = str(session_or_options or "").strip()
        processor_entity = ""

    if not session_id:
        raise ValueError("缺少 checkout session id")
    if not re.fullmatch(r"[A-Za-z0-9_]+", processor_entity or ""):
        processor_entity = "openai_llc"
    return f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"


def _normalize_korean_billing_profile(entry: dict) -> dict:
    normalized = {
        "full_name": str((entry or {}).get("full_name") or (entry or {}).get("fullName") or "").strip(),
        "country": normalize_billing_country((entry or {}).get("country") or "KR"),
        "state": str((entry or {}).get("state") or (entry or {}).get("province") or "").strip(),
        "city": str((entry or {}).get("city") or "").strip(),
        "address": str((entry or {}).get("address") or "").strip(),
        "address_line2": str((entry or {}).get("address_line2") or (entry or {}).get("addressLine2") or "").strip(),
        "zip_code": str((entry or {}).get("zip_code") or (entry or {}).get("postalCode") or "").strip(),
    }
    if normalized["country"] != "KR":
        normalized["country"] = "KR"
    return normalized


def _normalize_singapore_billing_profile(entry: dict) -> dict:
    normalized = {
        "full_name": str((entry or {}).get("full_name") or (entry or {}).get("fullName") or "").strip(),
        "country": normalize_billing_country((entry or {}).get("country") or "SG"),
        "state": str((entry or {}).get("state") or (entry or {}).get("province") or "").strip(),
        "city": str((entry or {}).get("city") or "Singapore").strip(),
        "address": str((entry or {}).get("address") or "").strip(),
        "address_line2": str((entry or {}).get("address_line2") or (entry or {}).get("addressLine2") or "").strip(),
        "zip_code": str((entry or {}).get("zip_code") or (entry or {}).get("postalCode") or "").strip(),
    }
    if normalized["country"] != "SG":
        normalized["country"] = "SG"
    if not normalized["state"]:
        normalized["state"] = "SG"
    if not normalized["city"]:
        normalized["city"] = "Singapore"
    return normalized


def _fallback_korean_billing_profiles() -> list:
    return [
        {
            "full_name": "김지훈",
            "country": "KR",
            "state": "Seoul",
            "city": "강남구",
            "address": "역삼로 310, 역삼푸르지오",
            "address_line2": "",
            "zip_code": "06225",
        },
        {
            "full_name": "이은주",
            "country": "KR",
            "state": "Seoul",
            "city": "서초구",
            "address": "서초중앙로 200, 아크로비스타",
            "address_line2": "",
            "zip_code": "06591",
        },
        {
            "full_name": "박서준",
            "country": "KR",
            "state": "Seoul",
            "city": "송파구",
            "address": "송파대로 345, 헬리오시티",
            "address_line2": "",
            "zip_code": "05838",
        },
        {
            "full_name": "최민수",
            "country": "KR",
            "state": "Seoul",
            "city": "용산구",
            "address": "한강대로 95, 래미안용산더중앙",
            "address_line2": "",
            "zip_code": "04378",
        },
        {
            "full_name": "정해인",
            "country": "KR",
            "state": "Seoul",
            "city": "마포구",
            "address": "백범로 205, 마포자이",
            "address_line2": "",
            "zip_code": "04147",
        },
    ]


def _fallback_singapore_billing_profiles() -> list:
    fallback_names = [
        "Tan Wei Lian",
        "Lee Xiu Qi",
        "Lim Kah Kee",
        "Wong Jun Jie",
        "Chen Meiling",
    ]
    fallback_entries = list(REAL_ADDRESS_DATABASE.get("SG") or [])
    profiles = []
    for index, entry in enumerate(fallback_entries):
        profiles.append(
            _normalize_singapore_billing_profile(
                {
                    "full_name": fallback_names[index % len(fallback_names)],
                    "country": "SG",
                    "state": str((entry or {}).get("state") or "SG").strip(),
                    "city": str((entry or {}).get("city") or "Singapore").strip(),
                    "address": str((entry or {}).get("address") or "").strip(),
                    "address_line2": str((entry or {}).get("address_line2") or "").strip(),
                    "zip_code": str((entry or {}).get("zip_code") or "").strip(),
                }
            )
        )
    return profiles


@lru_cache(maxsize=1)
def _load_korean_billing_profiles() -> list:
    profiles = []
    try:
        with open(KOREAN_BILLING_PROFILES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            raw = raw.get("profiles") or raw.get("items") or []
        for item in raw or []:
            normalized = _normalize_korean_billing_profile(item if isinstance(item, dict) else {})
            if normalized["full_name"] and normalized["address"] and normalized["city"] and normalized["zip_code"]:
                profiles.append(normalized)
    except Exception as exc:
        logger.log("Billing", f"加载韩国资料池失败，使用内置备用资料: {exc}", "WARN")

    if profiles:
        return profiles

    logger.log("Billing", "韩国资料池为空，使用内置备用资料", "WARN")
    return [_normalize_korean_billing_profile(item) for item in _fallback_korean_billing_profiles()]


@lru_cache(maxsize=1)
def _load_singapore_billing_profiles() -> list:
    profiles = []
    try:
        with open(SINGAPORE_BILLING_PROFILES_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            raw = raw.get("profiles") or raw.get("items") or []
        for item in raw or []:
            normalized = _normalize_singapore_billing_profile(item if isinstance(item, dict) else {})
            if normalized["full_name"] and normalized["address"] and normalized["city"] and normalized["zip_code"]:
                profiles.append(normalized)
    except Exception as exc:
        logger.log("Billing", f"加载新加坡资料池失败，使用内置备用资料: {exc}", "WARN")

    if profiles:
        return profiles

    logger.log("Billing", "新加坡资料池为空，使用内置备用资料", "WARN")
    return [_normalize_singapore_billing_profile(item) for item in _fallback_singapore_billing_profiles()]


GENERIC_MAILBOX_DOMAINS = {
    "gmail", "googlemail", "outlook", "hotmail", "live", "msn",
    "icloud", "me", "mac", "yahoo", "ymail", "aol", "proton",
    "protonmail", "qq", "163", "126", "foxmail", "sina", "sohu",
}


def _humanize_workspace_tokens(value: str) -> str:
    raw_tokens = re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(value or "").strip())
    tokens = [token for token in raw_tokens if token]
    if not tokens:
        return ""

    humanized = []
    for token in tokens[:4]:
        if re.fullmatch(r"[A-Za-z0-9]+", token):
            humanized.append(token[:1].upper() + token[1:].lower())
        else:
            humanized.append(token)
    return " ".join(humanized).strip()


def _extract_workspace_domain_brand(domain: str) -> str:
    labels = [one for one in str(domain or "").strip().lower().split(".") if one]
    if len(labels) < 2:
        return ""

    second_level_suffixes = {"co", "com", "net", "org", "gov", "edu", "ac"}
    if len(labels) >= 3 and labels[-2] in second_level_suffixes:
        return labels[-3]
    return labels[-2]


def _auto_workspace_date_suffix() -> str:
    return datetime.now().strftime("%Y%m%d")


def build_payment_workspace_name(email: str = "", workspace_name: str = "") -> str:
    clean_workspace = str(workspace_name or "").strip()
    if clean_workspace:
        return clean_workspace

    email_text = str(email or "").strip()
    local_part, _, domain = email_text.partition("@")
    domain_brand = _extract_workspace_domain_brand(domain)
    if domain_brand and domain_brand not in GENERIC_MAILBOX_DOMAINS:
        humanized_brand = _humanize_workspace_tokens(domain_brand)
        if humanized_brand:
            return f"{humanized_brand[:40]} Team {_auto_workspace_date_suffix()}"

    humanized_local = _humanize_workspace_tokens(local_part)
    if humanized_local:
        return f"{humanized_local[:40]} Team {_auto_workspace_date_suffix()}"
    return "My Team Workspace"


def build_business_checkout_referrer() -> str:
    return "https://chatgpt.com/?promo_campaign=team-1-month-free"


def build_chatgpt_checkout_cancel_url(plan_type: str = "business") -> str:
    return "https://chatgpt.com/#pricing"


def build_chatgpt_checkout_payload(plan_type: str = "business", email: str = "",
                                   workspace_name: str = "", seat_quantity: int = 5,
                                   country: str = "SG", currency: str = "USD") -> dict:
    clean_plan_type = normalize_plan_type(plan_type)
    clean_country = normalize_billing_country(country)
    clean_currency = normalize_currency(currency)

    if clean_plan_type == "plus":
        return {
            "plan_name": "chatgptplusplan",
            "billing_details": {
                "country": clean_country,
                "currency": clean_currency,
            },
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": True,
            },
            "checkout_ui_mode": "custom",
        }

    try:
        clean_seat_quantity = max(1, int(seat_quantity))
    except (TypeError, ValueError):
        clean_seat_quantity = 5

    return {
        "plan_name": "chatgptteamplan",
        "team_plan_data": {
            "workspace_name": build_payment_workspace_name(email=email, workspace_name=workspace_name),
            "price_interval": "month",
            "seat_quantity": clean_seat_quantity,
        },
        "billing_details": {
            "country": clean_country,
            "currency": clean_currency,
        },
        "cancel_url": build_chatgpt_checkout_cancel_url(clean_plan_type),
        "promo_campaign": {
            "promo_campaign_id": "team-1-month-free",
            "is_coupon_from_query_param": True,
        },
        "entry_point": "team_workspace_purchase_modal",
        "checkout_ui_mode": "custom",
    }


def build_chatgpt_checkout_referrer(plan_type: str = "business") -> str:
    if normalize_plan_type(plan_type) == "plus":
        return "https://chatgpt.com/?promo_campaign=plus-1-month-free"
    return build_business_checkout_referrer()


def generate_billing_profile(country: str = "SG", kr_success_mode: str | None = None) -> dict:
    clean_country = normalize_billing_country(country)
    selection = None
    if clean_country == "KR":
        selection = _get_kr_success_selection(clean_country, kr_success_mode)
        profile = dict(random.choice(_load_korean_billing_profiles()))
    elif clean_country == "SG":
        profile = dict(random.choice(_load_singapore_billing_profiles()))
    else:
        fake = Faker('en_US')
        entry_pool = list(REAL_ADDRESS_DATABASE.get(clean_country) or REAL_ADDRESS_DATABASE.get("US") or [])
        if entry_pool:
            entry = random.choice(entry_pool)
        else:
            entry = {
                "address": fake.street_address(),
                "city": fake.city(),
                "state": fake.state_abbr(),
                "zip_code": fake.postcode(),
                "address_line2": "",
            }
        profile = {
            "full_name": fake.name(),
            "address": entry["address"],
            "city": entry["city"],
            "state": entry["state"],
            "zip_code": entry["zip_code"],
            "address_line2": entry.get("address_line2", ""),
            "country": clean_country,
        }

    if selection:
        for key in ("full_name", "address", "address_line2", "city", "state", "zip_code"):
            value = selection.get(key)
            if value:
                profile[key] = value
        profile["country"] = "KR"
    return profile


def build_kr_success_profile_pool(rows: list[dict]) -> dict:
    pool = {
        "bin_prefixes": set(),
        "address_profiles_with_unit": [],
        "address_profiles_without_unit": [],
        "paired_profiles": [],
        "card_profiles": [],
        "bin_prefix_order": [],
        "_bin_prefix_seen": set(),
    }

    required_fields = ("address", "city", "state", "zip_code")

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        country = str(row.get("country") or "").strip().upper()
        if country != "KR":
            continue
        bin_prefix = str(row.get("bin_prefix") or "").strip()
        if bin_prefix:
            pool["bin_prefixes"].add(bin_prefix)
            if bin_prefix not in pool["_bin_prefix_seen"]:
                pool["bin_prefix_order"].append(bin_prefix)
                pool["_bin_prefix_seen"].add(bin_prefix)
        address = str(row.get("address") or "").strip()
        if not address:
            continue
        profile = {
            "full_name": str(row.get("full_name") or "").strip(),
            "address": address,
            "address_line2": str(row.get("address_line2") or "").strip(),
            "city": str(row.get("city") or "").strip(),
            "state": str(row.get("state") or "").strip(),
            "zip_code": str(row.get("zip_code") or "").strip(),
            "bin_prefix": bin_prefix,
            "card_number": str(row.get("card_number") or "").strip(),
            "card_last4": str(row.get("card_last4") or "").strip(),
            "expiry_date": str(row.get("expiry_date") or "").strip(),
            "cvv": str(row.get("cvv") or "").strip(),
        }
        if not all(profile[field] for field in required_fields):
            continue
        pool["card_profiles"].append(profile)
        has_unit = bool(UNIT_ADDRESS_RE.search(profile["address_line2"]))
        has_card_fields = all(profile[field] for field in ("card_number", "card_last4", "expiry_date", "cvv"))
        if has_unit:
            pool["address_profiles_with_unit"].append(profile)
        else:
            pool["address_profiles_without_unit"].append(profile)
        if has_unit and has_card_fields:
            pool["paired_profiles"].append(profile)
    return pool


def load_payment_success_rows(db_path: str) -> list[dict]:
    path = str(db_path or "").strip()
    if not path or not os.path.exists(path):
        return []

    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT country, bin_prefix, full_name, address, address_line2, city, state, zip_code,
                   card_number, card_last4, expiry_date, cvv
            FROM payment_success_records
            WHERE UPPER(country) = 'KR'
            """
        ).fetchall()
        return [
            {
                "country": row["country"],
                "bin_prefix": row["bin_prefix"],
                "full_name": row["full_name"],
                "address": row["address"],
                "address_line2": row["address_line2"],
                "city": row["city"],
                "state": row["state"],
                "zip_code": row["zip_code"],
                "card_number": row["card_number"],
                "card_last4": row["card_last4"],
                "expiry_date": row["expiry_date"],
                "cvv": row["cvv"],
            }
            for row in rows
        ]
    except Exception as exc:
        logger.log(
            "Billing",
            f"KR success rows load failed, fallback to empty pool: {path}; error: {exc}",
            "WARN",
        )
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _get_kr_success_selection(country: str = "SG", mode: str | None = None) -> dict | None:
    clean_country = normalize_billing_country(country)
    effective_mode = normalize_kr_success_profile_mode(mode if mode is not None else KR_SUCCESS_PROFILE_DEFAULT_MODE)
    if clean_country != "KR" or effective_mode == "disabled":
        return None
    rows = load_payment_success_rows(PAYMENT_SUCCESS_RECORDS_DB_PATH)
    if not rows:
        return None
    pool = build_kr_success_profile_pool(rows)
    if not pool.get("bin_prefixes"):
        return None
    try:
        selection = choose_kr_card_info_from_success_pool(
            pool,
            mode=effective_mode,
            fallback_card_info={"country": "KR"},
        )
        return selection
    except Exception as exc:
        logger.log("Billing", f"KR success selection failed: {exc}", "WARN")
    return None


def normalize_kr_success_profile_mode(mode: str | None = "split_priority") -> str:
    clean = str(mode or "split_priority").strip().lower()
    return clean if clean in {"disabled", "bin_only", "split_priority", "paired_reuse"} else "split_priority"


def choose_kr_card_info_from_success_pool(
    pool: dict,
    mode: str = "split_priority",
    fallback_card_info: dict | None = None,
) -> dict:
    fallback_base = dict(fallback_card_info or {})
    normalized_mode = normalize_kr_success_profile_mode(mode)
    bin_order = list(pool.get("bin_prefix_order") or [])
    bin_prefixes = pool.get("bin_prefixes") or set()
    if not isinstance(bin_prefixes, (list, tuple, set)):
        bin_prefixes = set(bin_prefixes)
    else:
        bin_prefixes = set(bin_prefixes)

    def _new_result() -> dict:
        return dict(fallback_base)

    def _apply_address_fields(profile: dict, result: dict) -> dict:
        for key in ("full_name", "address", "address_line2", "city", "state", "zip_code"):
            result[key] = profile.get(key, "")
        return result

    def _apply_full_profile(profile: dict) -> dict:
        result = dict(fallback_base)
        result.update(profile)
        return result

    def _preferred_bin(explicit: str | None = None) -> str | None:
        if explicit:
            return explicit
        if bin_order:
            return bin_order[0]
        if bin_prefixes:
            return sorted(bin_prefixes)[0]
        return None

    def _attach_bin(result: dict, explicit: str | None = None) -> dict:
        bin_value = _preferred_bin(explicit)
        if bin_value:
            result["bin_prefix"] = bin_value
        return result

    if normalized_mode == "disabled":
        return _new_result()

    if normalized_mode == "bin_only":
        return _attach_bin(_new_result())

    if normalized_mode == "split_priority":
        profile = None
        if pool.get("address_profiles_with_unit"):
            profile = pool["address_profiles_with_unit"][0]
        elif pool.get("address_profiles_without_unit"):
            profile = pool["address_profiles_without_unit"][0]
        result = _new_result()
        if profile:
            result = _apply_address_fields(profile, result)
        return _attach_bin(result)

    if normalized_mode == "paired_reuse":
        paired = pool.get("paired_profiles") or []
        if paired:
            return _apply_full_profile(paired[0])
        return choose_kr_card_info_from_success_pool(pool, mode="split_priority", fallback_card_info=fallback_base)

    return _new_result()



def generate_card_info(bin_prefix="625003", state='delaware', country: str = "SG", kr_success_mode: str | None = None) -> dict:
    clean_country = normalize_billing_country(country)
    normalized_mode = normalize_kr_success_profile_mode(kr_success_mode if kr_success_mode is not None else KR_SUCCESS_PROFILE_DEFAULT_MODE)
    selection = None
    if clean_country == "KR":
        selection = _get_kr_success_selection(clean_country, normalized_mode)
    if selection and normalized_mode == "paired_reuse" and selection.get("card_number"):
        result = dict(selection)
        if not result.get("card_number_formatted") and result.get("card_number"):
            result["card_number_formatted"] = _format_card_number(result["card_number"])
        result["country"] = "KR"
        return result
    bin_for_generation = selection.get("bin_prefix") if selection and selection.get("bin_prefix") else bin_prefix
    ci = CardGenerator.generate(bin_for_generation)
    billing_profile = generate_billing_profile(country=clean_country, kr_success_mode=normalized_mode)
    ci.update(billing_profile)
    ci["bin_prefix"] = bin_for_generation
    return ci


def rebuild_card_info_for_country(card_info: dict, country: str = "SG", kr_success_mode: str | None = None) -> dict:
    clean_country = normalize_billing_country(country)
    result = dict(card_info or {})
    selection = None
    if clean_country == "KR":
        selection = _get_kr_success_selection(clean_country, kr_success_mode)
    fields = ("full_name", "address", "address_line2", "city", "state", "zip_code")
    if selection:
        for key in fields:
            value = selection.get(key)
            if value:
                result[key] = value
        if selection.get("bin_prefix"):
            result["bin_prefix"] = selection["bin_prefix"]
        result["country"] = "KR"
        return result
    billing_profile = generate_billing_profile(country=clean_country, kr_success_mode=kr_success_mode)
    for key in fields:
        result[key] = billing_profile.get(key, "")
    if billing_profile.get("bin_prefix"):
        result["bin_prefix"] = billing_profile["bin_prefix"]
    result["country"] = clean_country
    return result


def rebuild_card_info_for_new_card(card_info: dict, bin_prefix: str = "625003") -> dict:
    generated = CardGenerator.generate(bin_prefix)
    result = dict(card_info or {})
    for key in ("card_number", "card_number_formatted", "expiry_date", "cvv", "bin_prefix"):
        result[key] = generated.get(key, result.get(key, ""))
    return result


def select_country_value(el, country_code: str = "SG") -> bool:
    """选择账单国家，优先按代码，再回退按文本"""
    clean_country = normalize_billing_country(country_code)
    country_text_map = {
        "SG": "Singapore",
        "KR": "South Korea",
        "US": "United States",
    }
    try:
        el.select.by_value(clean_country)
        return True
    except:
        pass
    try:
        el.select.by_text(country_text_map.get(clean_country, clean_country))
        return True
    except:
        pass
    return False


def normalize_paypal_profile_key(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized[:80]


def resolve_payment_browser_profile_dir(thread_id: str = None, paypal_profile_key: str = "") -> str:
    normalized_paypal_key = normalize_paypal_profile_key(paypal_profile_key)
    if normalized_paypal_key:
        return os.path.join(_BASE_DIR, "output", "browser_profiles", "paypal", normalized_paypal_key)

    import uuid

    profile_suffix = thread_id or str(uuid.uuid4())[:8]
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser(f"~/Library/Application Support/Chrome_Payment_{profile_suffix}")
    if system == "Windows":
        return os.path.expandvars(rf"%LOCALAPPDATA%\Chrome_Payment_{profile_suffix}")
    return os.path.expanduser(f"~/.config/Chrome_Payment_{profile_suffix}")


def resolve_effective_proxy_settings(
    *,
    proxy_url: str = "",
    use_proxy: bool = False,
    proxy_port: int | None = None,
    paypal_profile_key: str = "",
    paypal_profile_bypass_proxy: bool = False,
) -> tuple[str, bool, int | None]:
    clean_proxy_url = str(proxy_url or "").strip()
    effective_use_proxy = bool(use_proxy or clean_proxy_url)
    try:
        effective_proxy_port = int(proxy_port) if proxy_port not in (None, "") else None
    except (TypeError, ValueError):
        effective_proxy_port = None

    if paypal_profile_bypass_proxy and normalize_paypal_profile_key(paypal_profile_key):
        return "", False, None
    return clean_proxy_url, effective_use_proxy, effective_proxy_port


def _payment_profile_lock_path(profile_dir: str) -> str:
    return os.path.join(str(profile_dir or "").strip(), PAYPAL_PROFILE_LOCK_NAME)


def _payment_profile_pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def acquire_payment_profile_lock(profile_dir: str) -> dict:
    clean_profile_dir = str(profile_dir or "").strip()
    if not clean_profile_dir:
        raise RuntimeError("缺少浏览器 Profile 目录")
    os.makedirs(clean_profile_dir, exist_ok=True)
    lock_path = _payment_profile_lock_path(clean_profile_dir)

    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            lock_payload = {}
            try:
                with open(lock_path, "r", encoding="utf-8") as fh:
                    lock_payload = json.load(fh)
            except Exception:
                lock_payload = {}
            lock_pid = int(lock_payload.get("pid") or 0)
            if lock_pid > 0 and _payment_profile_pid_exists(lock_pid):
                raise RuntimeError(f"PayPal Profile 正在被占用（pid={lock_pid}）")
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                continue

    lock_info = {
        "pid": os.getpid(),
        "created_at": time.time(),
        "profile_dir": clean_profile_dir,
        "lock_path": lock_path,
    }
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(lock_info, fh, ensure_ascii=False)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.remove(lock_path)
        except Exception:
            pass
        raise
    return lock_info


def release_payment_profile_lock(lock_info: dict | None) -> None:
    if not lock_info:
        return
    lock_path = str((lock_info or {}).get("lock_path") or "").strip()
    if not lock_path:
        return
    try:
        os.remove(lock_path)
    except FileNotFoundError:
        pass


# ==================== Chrome 启动 ====================
def get_chrome_path():
    """获取系统 Chrome 路径"""
    system = platform.system()
    if system == "Darwin":
        paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif system == "Windows":
        paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        paths = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser"]

    for path in paths:
        if os.path.exists(path):
            return path
    return None


def create_browser(
    headless: bool = False,
    use_proxy: bool = False,
    proxy_port: int = None,
    thread_id: str = None,
    proxy_url: str = "",
    profile_dir: str = "",
    profile_debug_key: str = "",
) -> ChromiumPage:
    """创建 Chrome 浏览器"""
    step = "Chrome"
    options = ChromiumOptions()

    chrome_path = get_chrome_path()
    if chrome_path:
        options.set_browser_path(chrome_path)
        logger.log(step, f"使用 Chrome: {chrome_path}", "INFO")

    import hashlib
    temp_profile = str(profile_dir or "").strip() or resolve_payment_browser_profile_dir(thread_id=thread_id)
    logger.log(step, f"Profile 目录: {temp_profile}", "INFO")
    options.set_argument(f'--user-data-dir={temp_profile}')

    # 调试端口
    debug_key = str(profile_debug_key or "").strip() or str(thread_id or "").strip()
    if not debug_key:
        debug_key = os.path.basename(temp_profile)

    if debug_key:
        port_hash = int(hashlib.md5(debug_key.encode()).hexdigest()[:4], 16)
        debug_port = 9222 + (port_hash % 777)
    else:
        debug_port = 9222 + random.randint(0, 777)

    options.set_address(f'127.0.0.1:{debug_port}')
    logger.log(step, f"调试端口: {debug_port}", "INFO")

    # 代理设置
    clean_proxy_url = str(proxy_url or "").strip()
    if clean_proxy_url:
        options.set_argument(f'--proxy-server={clean_proxy_url}')
        logger.log(step, f"使用显式代理: {clean_proxy_url}", "INFO")
    elif use_proxy:
        port = proxy_port or PROXY_PORT
        options.set_argument(f'--proxy-server=http://{PROXY_HOST}:{port}')
        logger.log(step, f"使用显式代理: {PROXY_HOST}:{port}", "INFO")

    options.set_argument('--disable-blink-features=AutomationControlled')
    if headless:
        options.set_argument('--headless=new')
    options.set_argument('--window-size=1440,900')

    try:
        page = ChromiumPage(addr_or_opts=options)
        logger.log(step, "Chrome 已启动", "SUCCESS")
        return page
    except Exception as e:
        logger.log(step, f"创建浏览器失败: {e}", "ERROR")
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None


def close_payment_browser(
    page,
    *,
    profile_dir: str = "",
    browser_pid: int | None = None,
    thread_id: str | None = None,
    preserve_profile_session: bool = False,
    graceful_wait_seconds: float = 3.0,
) -> None:
    if not page:
        return

    step = "Payment"
    try:
        page.quit()
        logger.log(step, "Chrome 已关闭", "INFO")
    except Exception:
        pass

    if preserve_profile_session:
        wait_seconds = max(0.0, float(graceful_wait_seconds or 0.0))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            logger.log(step, f"PayPal Profile 已保留，等待写盘 {wait_seconds:.1f}s", "INFO")
        return

    try:
        if browser_pid:
            try:
                os.kill(browser_pid, signal.SIGTERM)
                time.sleep(0.5)
                os.kill(browser_pid, signal.SIGKILL)
            except Exception:
                pass

        profile_pattern = str(profile_dir or "").strip()
        if profile_pattern:
            subprocess.run(['pkill', '-9', '-f', profile_pattern], capture_output=True, timeout=5)
        elif thread_id:
            profile_name = f"Chrome_Payment_{thread_id}"
            subprocess.run(['pkill', '-9', '-f', profile_name], capture_output=True, timeout=5)
    except Exception:
        pass


def _build_hcaptcha_abort_result(
    page,
    step: str,
    card_info: dict,
    net_collector=None,
    *,
    result_detail: str,
    message: str,
    hcaptcha_info: dict | None = None,
    collect_requests_fn=None,
) -> dict:
    trace_path = None
    if net_collector:
        if collect_requests_fn is not None:
            try:
                captured = collect_requests_fn(page)
                if captured:
                    net_collector.mark_event(
                        "stripe_captured",
                        json.dumps(captured, ensure_ascii=False, default=str),
                    )
            except Exception:
                pass
        try:
            trace_path = net_collector.stop_and_save(success=False, result_detail=result_detail)
            if trace_path:
                logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
        except Exception:
            trace_path = None

    result = {
        "success": False,
        "message": message,
        "card_info": card_info,
        "hcaptcha_blocked": True,
    }
    if hcaptcha_info:
        result["hcaptcha_info"] = hcaptcha_info
    if trace_path:
        result["net_trace_file"] = trace_path
    return {"status": "abort", "result": result}


def handle_hcaptcha_challenge(
    page,
    step: str,
    card_info: dict,
    net_collector=None,
    collect_requests_fn=None,
):
    if _detect_hcaptcha is None:
        return None

    try:
        hc_info = _detect_hcaptcha(page)
    except Exception as exc:
        logger.log(step, f"hCaptcha 检测异常: {exc}", "WARN")
        return None

    if not hc_info or not hc_info.get("found"):
        return None

    hcaptcha_mode = (os.environ.get("HCAPTCHA_MODE", "abort") or "abort").strip().lower() or "abort"
    logger.log(step, f"🔐 检测到 Stripe hCaptcha 3DS 验证 (mode={hcaptcha_mode})", "WARN")

    if net_collector:
        try:
            net_collector.mark_event(
                "hcaptcha_detected",
                json.dumps(hc_info, ensure_ascii=False, default=str)[:500],
            )
        except Exception:
            pass

    if hcaptcha_mode == "manual":
        manual_wait_keys = getattr(handle_hcaptcha_challenge, "_manual_wait_keys", set())
        page_key = id(page)
        if page_key not in manual_wait_keys:
            logger.log(step, "检测到 hCaptcha，进入手动通过模式：保持页面，不退出，等待人工完成验证", "WARN")
            manual_wait_keys.add(page_key)
            setattr(handle_hcaptcha_challenge, "_manual_wait_keys", manual_wait_keys)
        return {
            "status": "continue",
            "message": "检测到 Stripe hCaptcha，等待手动通过（不退出）",
            "result": {
                "manual": True,
                "message": "等待手动通过 hCaptcha",
                "hcaptcha_info": hc_info,
            },
        }

    if hcaptcha_mode == "solve":
        api_key = os.environ.get("YESCAPTCHA_API_KEY", "")
        if not api_key:
            logger.log(step, "hcaptcha_mode=solve 但未配置 YESCAPTCHA_API_KEY", "ERROR")
            return _build_hcaptcha_abort_result(
                page,
                step,
                card_info,
                net_collector,
                result_detail="hCaptcha 自动解题失败",
                message="Stripe hCaptcha 已检测到，但未配置 YesCaptcha API Key",
                hcaptcha_info=hc_info,
                collect_requests_fn=collect_requests_fn,
            )

        if _solve_hcaptcha is None:
            logger.log(step, "hCaptcha solver 不可用", "ERROR")
            return _build_hcaptcha_abort_result(
                page,
                step,
                card_info,
                net_collector,
                result_detail="hCaptcha 自动解题失败",
                message="Stripe hCaptcha 已检测到，但本地未加载 hCaptcha solver",
                hcaptcha_info=hc_info,
                collect_requests_fn=collect_requests_fn,
            )

        try:
            hc_result = _solve_hcaptcha(page, api_key=api_key)
        except Exception as solve_err:
            logger.log(step, f"YesCaptcha 调用异常: {solve_err}", "ERROR")
            return _build_hcaptcha_abort_result(
                page,
                step,
                card_info,
                net_collector,
                result_detail="hCaptcha 自动解题失败",
                message=f"Stripe hCaptcha 自动解题异常: {solve_err}",
                hcaptcha_info=hc_info,
                collect_requests_fn=collect_requests_fn,
            )

        if hc_result.get("solved"):
            logger.log(step, "✓ hCaptcha 已解决", "SUCCESS")
            if net_collector:
                try:
                    net_collector.mark_event(
                        "hcaptcha_solved",
                        json.dumps(hc_result, ensure_ascii=False, default=str)[:500],
                    )
                except Exception:
                    pass
            return {
                "status": "continue",
                "message": hc_result.get("message", "hCaptcha 已解决"),
                "result": hc_result,
            }

        failure_message = hc_result.get("message", "") if isinstance(hc_result, dict) else str(hc_result)
        logger.log(step, f"✗ hCaptcha token 注入报告失败: {failure_message}", "WARN")

        # 即使 inject 报告失败，支付可能已经成功（invisible hCaptcha 自动通过）
        # 等几秒检查支付状态
        logger.log(step, "检查支付是否已完成（token 可能已通过其他途径生效）...", "INFO")
        time.sleep(5)

        try:
            if check_payment_success(page):
                logger.log(step, "✓ 支付已成功（hCaptcha 可能已自动通过）", "SUCCESS")
                return {
                    "status": "continue",
                    "message": "支付已成功（hCaptcha 自动通过）",
                    "result": hc_result,
                }
        except Exception:
            pass

        # 支付未完成，但不立即放弃 — 返回 continue 让循环继续检测
        logger.log(step, "支付未完成，继续等待...", "INFO")
        return {
            "status": "continue",
            "message": f"hCaptcha token 注入未确认，继续等待: {failure_message}",
            "result": hc_result,
        }

    logger.log(step, "当前卡/账号被风控标记，立即终止", "ERROR")
    return _build_hcaptcha_abort_result(
        page,
        step,
        card_info,
        net_collector,
        result_detail="hCaptcha 风控拦截",
        message="Stripe hCaptcha 风控拦截，当前卡被标记为高风险",
        hcaptcha_info=hc_info,
        collect_requests_fn=collect_requests_fn,
    )


def set_cookies(page, cookies, domain: str = "chatgpt.com", target_url: str = None):
    """设置 Cookies"""
    step = "Cookies"
    if not cookies:
        logger.log(step, "没有 Cookies", "ERROR")
        return {"success": False, "message": "没有 Cookies", "count": 0}

    normalized_cookies = _prepare_payment_cookies(cookies)
    cookie_list = _build_browser_cookie_payloads(normalized_cookies, domain=domain)
    has_session_cookie = any(
        (cookie.get("name") or "") == SESSION_COOKIE_NAME
        for cookie in cookie_list
    )

    # 检查 Cookie 数量
    if len(cookie_list) < 20 and not has_session_cookie:
        logger.log(step, f"Cookies 数量不足: {len(cookie_list)} < 20，判断为失败", "ERROR")
        return {"success": False, "message": f"Cookies 数量不足: {len(cookie_list)}", "count": len(cookie_list)}
    if len(cookie_list) < 20 and has_session_cookie:
        logger.log(step, f"Cookies 数量较少: {len(cookie_list)}，但检测到会话 Cookie，继续尝试", "WARN")

    page.get(f"https://{domain}/")
    time.sleep(2)

    set_count = 0
    for cookie in cookie_list:
        name = cookie.get('name', '') if isinstance(cookie, dict) else ''
        value = cookie.get('value', '') if isinstance(cookie, dict) else ''
        if not name or not value or name.startswith('__cf'):
            continue
        try:
            page.set.cookies(cookie)
            set_count += 1
        except:
            pass

    logger.log(step, f"设置了 {set_count} 个 Cookies", "SUCCESS" if set_count > 0 else "WARN")

    # 刷新页面使 Cookie 生效
    page.refresh()
    time.sleep(2)

    return {"success": True, "message": "Cookies 设置成功", "count": set_count}


# ==================== 核心：通过 API 导航到支付页面 ====================
def navigate_via_api(page, plan_type: str = "business", workspace_name: str = "", email: str = "",
                     seat_quantity: int = 5, country: str = "SG", currency: str = "USD",
                     checkout_country: str = "AUTO") -> dict:
    """
    通过直接调用后端 API 导航到支付页面（绕过 A/B 测试）

    Args:
        page: 浏览器页面对象
        plan_type: 套餐类型 (plus/business)
        workspace_name: 团队空间名称（仅 business）
        seat_quantity: 座位数量（仅 business）
        country: 账单国家代码
        currency: 货币代码

    Returns:
        dict: {"success": bool, "payment_url": str, "message": str}
    """
    step = "NavigateAPI"
    clean_plan_type = normalize_plan_type(plan_type)
    clean_country = normalize_billing_country(country)
    clean_currency = normalize_currency(currency)
    resolved_checkout_country = resolve_checkout_country(checkout_country, clean_country, "US")
    checkout_payload_country = resolved_checkout_country if clean_plan_type == "business" else clean_country
    checkout_payload_currency = (
        get_default_checkout_currency(clean_plan_type, checkout_payload_country)
        if clean_plan_type == "business"
        else clean_currency
    )
    pricing_countries = (
        build_pricing_config_country_sequence(checkout_payload_country, "US")
        if clean_plan_type == "business"
        else []
    )

    logger.log(step, "========== 通过 API 直接导航到支付页面 ==========", "INFO")
    logger.log(
        step,
        f"套餐类型: {clean_plan_type}, 地址国家: {clean_country}, 结账国家: {checkout_payload_country}, 货币: {checkout_payload_currency}",
        "INFO",
    )

    # 确保在 ChatGPT 主页，以便获取 accessToken
    current_url = page.url
    if 'chatgpt.com' not in current_url:
        logger.log(step, "导航到 ChatGPT 主页...", "INFO")
        page.get("https://chatgpt.com/")
        time.sleep(3)

    payload = build_chatgpt_checkout_payload(
        plan_type=clean_plan_type,
        email=email,
        workspace_name=workspace_name,
        seat_quantity=seat_quantity,
        country=checkout_payload_country,
        currency=checkout_payload_currency,
    )
    referrer = build_chatgpt_checkout_referrer(clean_plan_type)
    payload_json = json.dumps(payload, ensure_ascii=False)
    referrer_json = json.dumps(referrer, ensure_ascii=False)
    plan_type_json = json.dumps(clean_plan_type, ensure_ascii=False)
    pricing_countries_json = json.dumps(pricing_countries, ensure_ascii=False)

    # 构建完整的 JS 脚本
    js_code = f'''
    (async function() {{
        try {{
            window._apiResult = null;
            // 获取 accessToken
            const bootstrapScript = document.getElementById("client-bootstrap");
            if (!bootstrapScript) {{
                window._apiResult = {{ success: false, error: "未找到 client-bootstrap，页面可能未完全加载" }};
                return;
            }}

            let pageSession;
            try {{
                pageSession = JSON.parse(bootstrapScript.textContent);
            }} catch (e) {{
                window._apiResult = {{ success: false, error: "解析 client-bootstrap 失败: " + e.message }};
                return;
            }}

            const token = pageSession.session?.accessToken;
            if (!token) {{
                window._apiResult = {{ success: false, error: "未找到 accessToken，可能未登录或 Cookie 无效" }};
                return;
            }}

            console.log("✓ 获取到 accessToken");

            const planType = {plan_type_json};
            const payload = {payload_json};
            const pricingCountries = {pricing_countries_json};
            const cookieMap = {{}};
            for (const part of String(document.cookie || "").split(";")) {{
                const segment = String(part || "").trim();
                if (!segment) continue;
                const separatorIndex = segment.indexOf("=");
                const cookieName = separatorIndex >= 0 ? segment.slice(0, separatorIndex).trim() : segment;
                const cookieValue = separatorIndex >= 0 ? segment.slice(separatorIndex + 1).trim() : "";
                if (cookieName) {{
                    cookieMap[cookieName] = cookieValue;
                }}
            }}

            function buildBaseHeaders() {{
                const headers = {{
                    accept: "*/*",
                    "accept-language": "zh-CN,zh;q=0.9",
                    authorization: "Bearer " + token,
                }};
                const accountId = String(cookieMap._account || "").trim();
                if (accountId) headers["chatgpt-account-id"] = accountId;
                const deviceId = String(cookieMap["oai-did"] || "").trim();
                if (deviceId) headers["oai-device-id"] = deviceId;
                const language = String(navigator.language || "zh-CN").trim() || "zh-CN";
                headers["oai-language"] = language;
                try {{
                    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {{
                        headers["oai-session-id"] = crypto.randomUUID();
                    }}
                }} catch (error) {{}}
                const clientBuildNumber = String(
                    document.documentElement?.getAttribute?.("data-build-number")
                    || pageSession?.buildNumber
                    || pageSession?.clientBuildNumber
                    || ""
                ).trim();
                if (clientBuildNumber) headers["oai-client-build-number"] = clientBuildNumber;
                const clientVersion = String(
                    document.documentElement?.getAttribute?.("data-client-version")
                    || pageSession?.clientVersion
                    || pageSession?.buildId
                    || ""
                ).trim();
                if (clientVersion) headers["oai-client-version"] = clientVersion;
                const sentinelToken = String(
                    window.__OPENAI_SENTINEL_TOKEN__
                    || window.openaiSentinelToken
                    || pageSession?.openaiSentinelToken
                    || ""
                ).trim();
                if (sentinelToken) headers["openai-sentinel-token"] = sentinelToken;
                return headers;
            }}

            function readNestedString(source, paths) {{
                for (const path of paths || []) {{
                    let value = source;
                    for (const segment of path) {{
                        if (value == null || typeof value !== "object") {{
                            value = "";
                            break;
                        }}
                        value = value[segment];
                    }}
                    const normalized = String(value || "").trim();
                    if (normalized) return normalized;
                }}
                return "";
            }}

            function derivePricingContext(pricingPayload) {{
                const rawCountry = readNestedString(pricingPayload, [
                    ["billing_details", "country"],
                    ["country_code"],
                    ["countryCode"],
                    ["country"],
                    ["checkout", "billing_details", "country"],
                    ["checkout", "country_code"],
                ]).toUpperCase();
                const country = {json.dumps(sorted(list(SUPPORTED_CHECKOUT_COUNTRIES)), ensure_ascii=False)}.includes(rawCountry) ? rawCountry : "";
                const currency = readNestedString(pricingPayload, [
                    ["billing_details", "currency"],
                    ["currency_code"],
                    ["currencyCode"],
                    ["currency"],
                    ["checkout", "billing_details", "currency"],
                    ["checkout", "currency_code"],
                ]).toUpperCase();
                const processorEntity = readNestedString(pricingPayload, [
                    ["processor_entity"],
                    ["processorEntity"],
                    ["checkout", "processor_entity"],
                    ["checkout", "processorEntity"],
                    ["payment", "processor_entity"],
                ]);
                return {{ country, currency, processorEntity }};
            }}

            const baseHeaders = buildBaseHeaders();
            let pricingPrewarm = null;
            if (planType === "business" && Array.isArray(pricingCountries) && pricingCountries.length) {{
                const attempts = [];
                for (const countryCode of pricingCountries) {{
                    try {{
                        const response = await fetch(
                            "https://chatgpt.com/backend-api/checkout_pricing_config/configs/" + encodeURIComponent(countryCode),
                            {{
                                headers: {{
                                    ...baseHeaders,
                                    "x-openai-target-route": "/backend-api/checkout_pricing_config/configs/{{country_code}}",
                                    "x-openai-target-path": "/backend-api/checkout_pricing_config/configs/" + countryCode,
                                }},
                                referrer: {referrer_json},
                                method: "GET",
                                mode: "cors",
                                credentials: "include",
                            }}
                        );
                        const rawText = await response.text();
                        let parsed = null;
                        try {{
                            parsed = rawText ? JSON.parse(rawText) : null;
                        }} catch (error) {{}}
                        attempts.push({{
                            country: countryCode,
                            ok: response.ok,
                            status: response.status,
                            payload: parsed,
                        }});
                    }} catch (error) {{
                        attempts.push({{
                            country: countryCode,
                            ok: false,
                            status: 0,
                            error: error?.message || String(error),
                            payload: null,
                        }});
                    }}
                }}
                const successfulAttempts = attempts.filter((attempt) => attempt.ok && attempt.payload);
                const lastSuccessfulAttempt = successfulAttempts.length
                    ? successfulAttempts[successfulAttempts.length - 1]
                    : null;
                const derivedContext = lastSuccessfulAttempt
                    ? derivePricingContext(lastSuccessfulAttempt.payload)
                    : {{ country: "", currency: "", processorEntity: "" }};
                pricingPrewarm = {{
                    success: !!lastSuccessfulAttempt,
                    successfulCountries: successfulAttempts.map((attempt) => attempt.country),
                    pricingConfigPayload: lastSuccessfulAttempt?.payload || null,
                    country: derivedContext.country,
                    currency: derivedContext.currency,
                    processorEntity: derivedContext.processorEntity,
                    attempts,
                }};
                if (pricingPrewarm.country) payload.billing_details.country = pricingPrewarm.country;
                if (pricingPrewarm.currency) payload.billing_details.currency = pricingPrewarm.currency;
            }}

            console.log("发送支付 API 请求...", payload);

            // 调用支付 API
            const response = await fetch("https://chatgpt.com/backend-api/payments/checkout", {{
                headers: {{
                    ...baseHeaders,
                    "content-type": "application/json",
                    "x-openai-target-path": "/backend-api/payments/checkout",
                    "x-openai-target-route": "/backend-api/payments/checkout",
                }},
                referrer: {referrer_json},
                body: JSON.stringify(payload),
                method: "POST",
                mode: "cors",
                credentials: "include"
            }});

            const rawText = await response.text();
            let res = null;
            try {{
                res = rawText ? JSON.parse(rawText) : null;
            }} catch (error) {{}}
            const apiKeys = res && typeof res === "object" ? Object.keys(res) : [];
            console.log("API 响应:", res);
            console.log("API 响应 keys:", apiKeys);
            console.log("client_secret 存在:", !!(res && res.client_secret), "长度:", ((res && res.client_secret) || '').length);
            console.log("publishable_key 存在:", !!(res && res.publishable_key), "长度:", ((res && res.publishable_key) || '').length);

            if (!response.ok) {{
                window._apiResult = {{
                    success: false,
                    error: "API 返回无效响应: " + String(rawText || JSON.stringify(res || {{}})),
                    pricing_prewarm: pricingPrewarm,
                }};
                return;
            }}

            if (res && res.checkout_session_id) {{
                const processorEntity = String(
                    res.processor_entity
                    || res.processorEntity
                    || pricingPrewarm?.processorEntity
                    || ""
                ).trim();
                const checkoutUrl = String(res.url || "").trim()
                    || (processorEntity
                        ? "https://chatgpt.com/checkout/" + processorEntity + "/" + res.checkout_session_id
                        : "https://chatgpt.com/checkout/openai_llc/" + res.checkout_session_id);
                window._apiResult = {{
                    success: true,
                    url: checkoutUrl,
                    session_id: res.checkout_session_id,
                    checkout_ui_mode: res.checkout_ui_mode || '',
                    client_secret: String(res.client_secret || ''),
                    publishable_key: String(res.publishable_key || ''),
                    processor_entity: processorEntity,
                    country: String(payload?.billing_details?.country || ''),
                    currency: String(payload?.billing_details?.currency || ''),
                    pricing_prewarm: pricingPrewarm,
                    api_keys: apiKeys.join(','),
                }};
                console.log("✓ 支付页面 URL:", checkoutUrl);
                console.log("✓ _apiResult.client_secret 长度:", window._apiResult.client_secret.length);
            }} else {{
                window._apiResult = {{ 
                    success: false, 
                    error: "API 返回无效响应: " + JSON.stringify(res),
                    pricing_prewarm: pricingPrewarm,
                }};
            }}
        }} catch (err) {{
            console.error("API 调用失败:", err);
            window._apiResult = {{ success: false, error: err.message }};
        }}
    }})();
    '''

    try:
        _wait_for_page_ready(page, timeout=12.0, poll_interval=0.5)
    except Exception as e:
        logger.log(step, f"等待页面稳定异常: {e}", "WARN")

    # 执行 JS 脚本
    logger.log(step, "执行 API 调用脚本...", "INFO")
    last_run_error = None
    for attempt in range(2):
        try:
            page.run_js(js_code)
            last_run_error = None
            break
        except Exception as e:
            last_run_error = e
            if attempt == 0 and _is_page_refresh_retryable_error(e):
                logger.log(step, f"页面刷新中，等待稳定后重试 API 调用脚本: {e}", "WARN")
                try:
                    _wait_for_page_ready(page, timeout=15.0, poll_interval=0.5)
                except Exception as wait_exc:
                    logger.log(step, f"重试前等待页面稳定异常: {wait_exc}", "WARN")
                time.sleep(1)
                continue
            logger.log(step, f"执行 JS 脚本失败: {e}", "ERROR")
            return {"success": False, "message": f"执行脚本失败: {e}"}
    if last_run_error is not None:
        logger.log(step, f"执行 JS 脚本失败: {last_run_error}", "ERROR")
        return {"success": False, "message": f"执行脚本失败: {last_run_error}"}

    # 等待异步操作完成
    time.sleep(3)

    # 获取结果
    result = None
    max_retries = 10
    for i in range(max_retries):
        try:
            result = page.run_js("return window._apiResult")
            if result:
                break
        except:
            pass
        time.sleep(1)

    if not result:
        try:
            _wait_for_page_ready(page, timeout=8.0, poll_interval=0.5)
            result = page.run_js("return window._apiResult")
        except Exception:
            pass

    if not result:
        logger.log(step, "未能获取 API 调用结果", "ERROR")
        return {"success": False, "message": "API 调用超时或失败"}

    logger.log(step, f"API 调用结果: {result}", "INFO")

    if result.get("success"):
        checkout_url = str(result.get("url") or "").strip()
        if not checkout_url:
            try:
                checkout_url = build_chatgpt_checkout_url(result, clean_plan_type)
            except Exception:
                checkout_url = ""
        if not checkout_url:
            return {"success": False, "message": "未能生成有效的支付链接"}
        client_secret = result.get("client_secret", "")
        publishable_key = result.get("publishable_key", "")
        api_keys = result.get("api_keys", "")
        effective_checkout_country = str(result.get("country") or checkout_payload_country).strip().upper() or checkout_payload_country
        logger.log(step, f"✓ 获取到支付页面 URL: {checkout_url}", "SUCCESS")
        logger.log(step, f"client_secret={'有' if client_secret else '无'}({len(client_secret)}字符) publishable_key={'有' if publishable_key else '无'}({len(publishable_key)}字符) api_keys=[{api_keys}]", "INFO")

        # 优先尝试内联 Stripe Checkout（不离开当前页面，绕过 431）
        if client_secret and publishable_key:
            logger.log(step, "尝试内联 Stripe Custom Checkout（绕过 checkout 页面 431）...", "INFO")
            inline_result = inject_stripe_checkout(page, publishable_key, client_secret)
            if inline_result.get("success"):
                logger.log(step, "✓ 内联 Stripe Checkout 加载成功", "SUCCESS")
                return {"success": True, "payment_url": checkout_url, "inline_checkout": True}
            else:
                logger.log(step, f"内联 Stripe Checkout 失败: {inline_result.get('message')}，回退到页面导航", "WARN")
        else:
            logger.log(step, f"payments/checkout 未返回 client_secret/publishable_key，使用页面导航", "WARN")
            # 把原始 _apiResult 里所有 key 打出来帮助调试
            try:
                raw_keys = page.run_js("return Object.keys(window._apiResult || {})")
                logger.log(step, f"_apiResult keys: {raw_keys}", "INFO")
            except Exception:
                pass

        # 回退：导航到支付页面
        for attempt in range(2):
            removed_count = _prune_checkout_request_cookies(page, domain="chatgpt.com")
            logger.log(step, f"导航到支付页面... (attempt={attempt + 1}, 清理 cookies={removed_count})", "INFO")
            page.get(checkout_url)
            time.sleep(3)

            # 强制切换 checkout 页面的国家到目标国家（Stripe 默认根据 IP 选国家）
            try:
                _force_country_js = '''
                (async function() {
                    var delay = function(ms) { return new Promise(function(r) { setTimeout(r, ms); }); };
                    // 等待页面和 Stripe iframe 加载完成
                    for (var wait = 0; wait < 15; wait++) {
                        var sels = ['#Field-countryInput','select[name="billingCountry"]','select[name="country"]','select[autocomplete="country"]'];
                        var found = false;
                        // 先在主页面找
                        for (var i = 0; i < sels.length; i++) {
                            var el = document.querySelector(sels[i]);
                            if (el) { found = true; break; }
                        }
                        // 在所有 iframe 里找
                        if (!found) {
                            var iframes = document.querySelectorAll('iframe');
                            for (var fi = 0; fi < iframes.length; fi++) {
                                try {
                                    var doc = iframes[fi].contentDocument;
                                    if (!doc) continue;
                                    for (var j = 0; j < sels.length; j++) {
                                        var el2 = doc.querySelector(sels[j]);
                                        if (el2) { found = true; el = el2; break; }
                                    }
                                    if (found) break;
                                } catch(e) {}
                            }
                        }
                        if (found && el) {
                            var ns = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
                            if (ns) ns.call(el, "''' + effective_checkout_country + '''");
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            return {success: true, waited: wait};
                        }
                        await delay(1000);
                    }
                    return {success: false, message: 'country select not found'};
                })();
                '''
                result = page.run_js(_force_country_js)
                if result and result.get("success"):
                    logger.log(step, f"✓ 已强制切换国家到 {effective_checkout_country} (等待 {result.get('waited', 0)}s)", "SUCCESS")
                    time.sleep(1)
                else:
                    logger.log(step, f"国家选择器未找到: {result}", "WARN")
            except Exception as e:
                logger.log(step, f"切换国家异常: {e}", "WARN")

            validation = _validate_checkout_page(page, expected_url=checkout_url)
            if validation.get("success"):
                current_url = validation.get("current_url") or checkout_url
                logger.log(step, f"✓ 成功到达支付页面: {current_url}", "SUCCESS")
                result_payload = {"success": True, "payment_url": checkout_url, "current_url": current_url}
                if validation.get("message"):
                    result_payload["message"] = validation["message"]
                return result_payload

            logger.log(
                step,
                f"checkout 页面异常: {validation.get('message')} | title={validation.get('title', '')}",
                "WARN" if attempt == 0 else "ERROR",
            )
            if attempt == 0:
                time.sleep(1)
                continue
            return {
                "success": False,
                "message": validation.get("message", "checkout 页面异常"),
                "current_url": validation.get("current_url", ""),
            }
    else:
        error_msg = result.get("error", "未知错误")
        logger.log(step, f"✗ API 调用失败: {error_msg}", "ERROR")
        return {"success": False, "message": error_msg}


# ==================== 内联 Stripe Custom Checkout ====================
def inject_stripe_checkout(page, publishable_key: str, client_secret: str) -> dict:
    """
    在当前 chatgpt.com 页面上直接注入 Stripe Custom Checkout 支付表单。
    绕过 /checkout/openai_llc/ 页面加载（避免 431 拦截）。

    流程:
      1. 创建容器 DOM
      2. 动态加载 js.stripe.com/v3/
      3. Stripe(pk).initCustomCheckout({ clientSecret })
      4. checkout.createElement('payment').mount(container)
    """
    step = "InlineStripe"

    # 先用 run_js 把参数安全地设置到 window 上，避免字符串拼接出错
    try:
        page.run_js(f"window.__stripe_pk = {json.dumps(publishable_key)};")
        page.run_js(f"window.__stripe_cs = {json.dumps(client_secret)};")
    except Exception as e:
        logger.log(step, f"设置参数失败: {e}", "ERROR")
        return {"success": False, "message": f"设置参数失败: {e}"}

    init_js = '''
    (async function() {
        try {
            var container = document.getElementById('stripe-inline-root');
            if (!container) {
                container = document.createElement('div');
                container.id = 'stripe-inline-root';
                container.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99990;background:#f5f5f5;overflow:auto;display:flex;justify-content:center;align-items:flex-start;padding:40px 20px;';
                container.innerHTML = '<div style="width:100%;max-width:480px;background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,0.12);padding:32px;margin-top:20px;">'
                    + '<h2 style="margin:0 0 20px;font-size:18px;color:#333;">Complete your payment</h2>'
                    + '<div id="stripe-payment-element" style="min-height:200px;"></div>'
                    + '<div id="stripe-status" style="margin-top:16px;font-size:13px;color:#666;text-align:center;"></div></div>';
                document.body.appendChild(container);
            }
            var statusEl = document.getElementById('stripe-status');
            if (statusEl) statusEl.textContent = 'Loading Stripe...';

            if (!window.Stripe) {
                await new Promise(function(resolve, reject) {
                    var s = document.createElement('script');
                    s.src = 'https://js.stripe.com/basil/stripe.js';
                    s.onload = resolve;
                    s.onerror = function() { reject(new Error('stripe.js load failed')); };
                    document.head.appendChild(s);
                });
            }

            if (statusEl) statusEl.textContent = 'Initializing...';
            var stripe = window.Stripe(window.__stripe_pk);

            // 检测可用的初始化方法
            var checkout = null;
            var initMethod = '';
            if (typeof stripe.initCustomCheckout === 'function') {
                checkout = await stripe.initCustomCheckout({ clientSecret: window.__stripe_cs });
                initMethod = 'initCustomCheckout';
            } else if (typeof stripe.initEmbeddedCheckout === 'function') {
                checkout = await stripe.initEmbeddedCheckout({ clientSecret: window.__stripe_cs });
                initMethod = 'initEmbeddedCheckout';
            }

            if (!checkout) {
                // 列出 stripe 对象上的所有方法帮助调试
                var methods = [];
                for (var k in stripe) {
                    if (typeof stripe[k] === 'function') methods.push(k);
                }
                window._stripeCheckoutError = 'No init method found. Available: ' + methods.join(', ');
                if (statusEl) statusEl.textContent = window._stripeCheckoutError;
                return;
            }

            window._stripeCheckout = checkout;
            window._stripeInitMethod = initMethod;

            // mount 或 直接使用
            if (initMethod === 'initEmbeddedCheckout') {
                // Embedded Checkout 自动 mount
                checkout.mount('#stripe-payment-element');
                window._stripeCheckoutReady = true;
            } else {
                var paymentElement = checkout.createElement('payment');
                paymentElement.mount('#stripe-payment-element');
                paymentElement.on('ready', function() {
                    window._stripeCheckoutReady = true;
                });
                paymentElement.on('change', function(event) {
                    window._stripeElementComplete = event.complete || false;
                });
            }

            if (statusEl) statusEl.textContent = 'Waiting for form (' + initMethod + ')...';

            var waited = 0;
            while (!window._stripeCheckoutReady && waited < 15000) {
                await new Promise(function(r) { setTimeout(r, 200); });
                waited += 200;
            }
            if (!window._stripeCheckoutReady) {
                window._stripeCheckoutError = 'Payment Element timeout (' + initMethod + ')';
                return;
            }
            window._stripeCheckoutError = null;
            if (statusEl) statusEl.textContent = 'Ready (' + initMethod + ')';
        } catch (err) {
            window._stripeCheckoutError = err.message || String(err);
            var s = document.getElementById('stripe-status');
            if (s) s.textContent = 'Error: ' + err.message;
        }
    })();
    '''

    try:
        page.run_js(init_js)
    except Exception as e:
        logger.log(step, f"注入 JS 失败: {e}", "ERROR")
        return {"success": False, "message": f"注入失败: {e}"}

    # 等待初始化完成
    for attempt in range(20):
        time.sleep(1)
        try:
            ready = page.run_js("return window._stripeCheckoutReady === true")
            if ready:
                logger.log(step, "Stripe Payment Element 已就绪", "SUCCESS")
                return {"success": True}
            error = page.run_js("return window._stripeCheckoutError")
            if error:
                logger.log(step, f"Stripe 初始化错误: {error}", "ERROR")
                return {"success": False, "message": str(error)}
        except Exception:
            pass

    logger.log(step, "Stripe Payment Element 加载超时", "ERROR")
    return {"success": False, "message": "加载超时"}


def stripe_inline_confirm(page) -> dict:
    """调用内联 Stripe Checkout 的 confirm() 完成支付。"""
    step = "InlineConfirm"

    confirm_js = '''
    (async function() {
        try {
            if (!window._stripeCheckout) {
                window._stripeConfirmResult = { success: false, error: 'no checkout instance' };
                return;
            }
            const statusEl = document.getElementById('stripe-status');
            if (statusEl) statusEl.textContent = '正在提交支付...';

            const result = await window._stripeCheckout.confirm({
                return_url: 'https://chatgpt.com/payments/success-team'
            });

            if (result && result.error) {
                console.error('[InlineConfirm] confirm error:', result.error);
                window._stripeConfirmResult = {
                    success: false,
                    error: result.error.message || JSON.stringify(result.error),
                    code: result.error.code || '',
                    decline_code: result.error.decline_code || '',
                    type: result.error.type || '',
                };
                if (statusEl) statusEl.textContent = '支付失败: ' + (result.error.message || '');
            } else {
                // confirm 成功后，Stripe 会自动重定向到 return_url
                // 如果没有重定向（例如 3DS 流程），标记为 pending
                window._stripeConfirmResult = { success: true, status: 'confirming' };
                if (statusEl) statusEl.textContent = '支付处理中...';
            }
        } catch (err) {
            console.error('[InlineConfirm] exception:', err);
            window._stripeConfirmResult = { success: false, error: err.message || String(err) };
        }
    })();
    '''

    try:
        page.run_js(confirm_js)
    except Exception as e:
        logger.log(step, f"confirm 执行异常: {e}", "ERROR")
        return {"success": False, "error": str(e)}

    # 等待 confirm 结果（最多 30 秒，因为可能有 3DS）
    for attempt in range(30):
        time.sleep(1)
        try:
            result = page.run_js("return window._stripeConfirmResult")
            if result:
                if result.get("success"):
                    logger.log(step, "✓ Stripe confirm 成功", "SUCCESS")
                else:
                    decline_code = result.get("decline_code", "")
                    error_msg = result.get("error", "未知错误")
                    logger.log(step, f"✗ Stripe confirm 失败: {error_msg} (decline_code={decline_code})", "ERROR")
                return result
        except Exception:
            pass

        # 如果页面已经跳转到成功 URL，说明支付完成
        try:
            if check_payment_success(page):
                logger.log(step, "✓ 检测到支付成功（页面已跳转）", "SUCCESS")
                return {"success": True, "status": "redirected"}
        except Exception:
            pass

    logger.log(step, "confirm 超时", "WARN")
    return {"success": False, "error": "confirm 超时"}


def navigate_to_checkout_url(page, checkout_url: str) -> dict:
    """Cookie 生效后直接跳转到外部传入的支付链接。"""
    step = "CheckoutURL"
    target_url = str(checkout_url or "").strip()
    if not target_url:
        return {"success": False, "message": "checkout_url 为空"}

    logger.log(step, f"使用外部 checkout_url: {target_url}", "INFO")
    for attempt in range(2):
        removed_count = _prune_checkout_request_cookies(page, domain="chatgpt.com")
        try:
            logger.log(step, f"跳转 checkout_url... (attempt={attempt + 1}, 清理 cookies={removed_count})", "INFO")
            page.get(target_url)
            time.sleep(3)
        except Exception as e:
            logger.log(step, f"跳转 checkout_url 失败: {e}", "ERROR")
            return {"success": False, "message": f"跳转失败: {e}"}

        validation = _validate_checkout_page(page, expected_url=target_url)
        if validation.get("success"):
            current_url = validation.get("current_url") or target_url
            logger.log(step, f"✓ 成功到达支付页面: {current_url}", "SUCCESS")
            return {"success": True, "payment_url": target_url, "current_url": current_url}

        logger.log(
            step,
            f"checkout 页面异常: {validation.get('message')} | title={validation.get('title', '')}",
            "WARN" if attempt == 0 else "ERROR",
        )
        if attempt == 0:
            time.sleep(1)
            continue
        return {
            "success": False,
            "message": validation.get("message", "checkout 页面异常"),
            "current_url": validation.get("current_url", ""),
        }


# ==================== 表单填充 ====================
def _normalize_fill_mode(fill_mode: str = "all") -> str:
    clean_mode = str(fill_mode or "all").strip().lower()
    return clean_mode if clean_mode in {"all", "card_only", "billing_only"} else "all"


def _resolve_fill_context_modes(fill_mode: str = "all") -> tuple[str, str]:
    normalized_fill_mode = _normalize_fill_mode(fill_mode)
    if normalized_fill_mode == "all":
        # 主页面先尝试账单字段，减少不必要的卡片焦点跳动；
        # 但 iframe 仍必须保留账单回填能力，因为 hosted checkout
        # 的地址字段有时只存在于 iframe 内。
        return "billing_only", "all"
    return normalized_fill_mode, normalized_fill_mode


def _build_fill_js(card_info: dict, email: str = "", fill_mode: str = "all") -> str:
    """构建注入到 iframe 的一体化填充 JS 脚本（参考 AI填卡助手 v1.4.1）"""
    normalized_fill_mode = _normalize_fill_mode(fill_mode)
    should_fill_card = normalized_fill_mode != "billing_only"
    should_fill_billing = normalized_fill_mode != "card_only"
    card_number = card_info['card_number'].replace(' ', '')
    expiry = card_info['expiry_date']
    cvv = card_info['cvv']
    full_name = card_info.get('full_name', '').replace("'", "\\'").replace('"', '\\"')
    country = card_info.get('country', 'SG')
    state = card_info.get('state', '').replace("'", "\\'")
    city = card_info.get('city', '').replace("'", "\\'")
    address = card_info.get('address', '').replace("'", "\\'")
    address_line2 = card_info.get('address_line2', '').replace("'", "\\'").replace('"', '\\"')
    zip_code = card_info.get('zip_code', '')
    email_escaped = email.replace("'", "\\'") if email else ''

    return '''
(async function() {
    const delay = ms => new Promise(r => setTimeout(r, ms));
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    let cardFilled = 0, addrFilled = 0;
    const shouldFillCard = ''' + ('true' if should_fill_card else 'false') + ''';
    const shouldFillBilling = ''' + ('true' if should_fill_billing else 'false') + ''';

    // ---- 查找字段 ----
    function find(selectors) {
        for (const s of selectors) {
            const el = document.querySelector(s);
            if (el) return el;
        }
        return null;
    }

    function clickIfExists(selectors) {
        const el = find(selectors);
        if (!el) return false;
        try { el.click(); return true; } catch (e) {}
        try {
            el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return true;
        } catch (e) {}
        return false;
    }

    // ---- Stripe 逐字符模拟（卡片字段必须） ----
    function typeChars(el, value) {
        el.focus();
        el.dispatchEvent(new FocusEvent('focus', {bubbles: true}));
        // 清空
        if (nativeSetter) nativeSetter.call(el, '');
        const t = el._valueTracker; if (t) t.setValue('');
        el.dispatchEvent(new Event('input', {bubbles: true}));

        for (let i = 0; i < value.length; i++) {
            const ch = value[i], cc = ch.charCodeAt(0);
            el.dispatchEvent(new KeyboardEvent('keydown',  {key:ch, code:'Key'+ch.toUpperCase(), charCode:cc, keyCode:cc, which:cc, bubbles:true, cancelable:true}));
            el.dispatchEvent(new KeyboardEvent('keypress', {key:ch, code:'Key'+ch.toUpperCase(), charCode:cc, keyCode:cc, which:cc, bubbles:true, cancelable:true}));
            const cur = el.value + ch;
            if (nativeSetter) nativeSetter.call(el, cur); else el.value = cur;
            const tk = el._valueTracker; if (tk) tk.setValue(cur.slice(0,-1));
            el.dispatchEvent(new InputEvent('input', {data:ch, inputType:'insertText', bubbles:true, cancelable:true}));
            el.dispatchEvent(new KeyboardEvent('keyup', {key:ch, code:'Key'+ch.toUpperCase(), charCode:cc, keyCode:cc, which:cc, bubbles:true, cancelable:true}));
        }
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
    }

    // ---- 快速设值（非 Stripe 字段：姓名、地址等） ----
    function fastSet(el, value) {
        el.focus();
        el.dispatchEvent(new FocusEvent('focus', {bubbles:true}));
        if (nativeSetter) nativeSetter.call(el, value); else el.value = value;
        const t = el._valueTracker; if (t) t.setValue('');
        el.dispatchEvent(new Event('input', {bubbles:true}));
        el.dispatchEvent(new Event('change', {bubbles:true}));
        el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true}));
        el.dispatchEvent(new FocusEvent('blur', {bubbles:true}));
    }

    // ---- select 下拉框 ----
    function setSelect(el, value, textFallback) {
        // 尝试 by value
        const opt = el.querySelector('option[value="' + value + '"]');
        if (opt) { el.value = value; el.dispatchEvent(new Event('change',{bubbles:true})); return true; }
        // 尝试 by text
        if (textFallback) {
            for (const o of el.querySelectorAll('option')) {
                if (o.textContent.trim().toLowerCase().includes(textFallback.toLowerCase())) {
                    el.value = o.value; el.dispatchEvent(new Event('change',{bubbles:true})); return true;
                }
            }
        }
        return false;
    }

    const isStripe = window.location.origin.includes('stripe.com') || window.location.origin.includes('stripecdn.com');
    const countryTextMap = {"SG":"Singapore","KR":"South Korea","US":"United States"};

    async function prepareCheckoutForm() {
        const clickedCardAccordion = clickIfExists(['button[data-testid="card-accordion-item-button"]']);
        if (clickedCardAccordion) {
            await delay(120);
        }

        const hostedSwitch = find(['.HostedSwitch']);
        if (hostedSwitch && hostedSwitch.getAttribute('aria-checked') !== 'true') {
            clickIfExists(['.HostedSwitch']);
            await delay(80);
        }

        const manualEntryClicked = clickIfExists([
            '.AddressAutocomplete-manual-entry.Button',
            '.AddressAutocomplete-manual-entry .Button'
        ]);
        if (manualEntryClicked) {
            await delay(120);
        }
    }

    async function waitForStateOptions() {
        const stateSel = [
            '#Field-administrativeAreaInput',
            'select[id$="-administrativeAreaInput"]',
            'select[name="billingAdministrativeArea"]',
            'select[name="administrativeArea"]',
            'select[name="state"]',
            'select[autocomplete="address-level1"]',
            'select[autocomplete~="address-level1"]'
        ];
        const maxWait = 2000;
        const poll = 120;
        const t0 = Date.now();
        while (Date.now() - t0 < maxWait) {
            let ready = false;
            for (const s of stateSel) {
                const el = document.querySelector(s);
                if (el && el.tagName === 'SELECT' && el.querySelectorAll('option').length > 1) {
                    ready = true;
                    break;
                }
            }
            if (ready) break;
            await delay(poll);
        }
    }

    async function fillTextField(selectors, value, options = {}) {
        if (value === undefined || value === null || value === '') return false;
        const el = find(selectors);
        if (!el) return false;

        const skipIfFilled = options.skipIfFilled !== false;
        const cur = String(el.value || '').trim();
        if (skipIfFilled && cur) return false;

        fastSet(el, String(value));
        await delay(options.delayMs || 40);
        return true;
    }

    async function fillSelectField(selectors, value, textFallback, options = {}) {
        if (value === undefined || value === null || value === '') return false;
        const el = find(selectors);
        if (!el) return false;
        const cur = String(el.value || '').trim();
        if (options.skipIfFilled !== false && cur && cur.toLowerCase() === String(value).toLowerCase()) {
            return false;
        }
        const ok = setSelect(el, String(value), textFallback || String(value));
        if (ok) await delay(options.delayMs || 40);
        return ok;
    }

    async function fillNonCardFields() {
        if (await fillTextField(['#email','input[name="email"]','input[type="email"]','input[autocomplete="email"]'], "''' + email_escaped + '''", {delayMs: 30})) {
            addrFilled++;
        }

        if (await fillTextField(['#Field-nameInput','input[name="billingName"]','input[name="name"]','input[autocomplete="name"]','input[autocomplete="cc-name"]'], "''' + full_name + '''", {skipIfFilled: false})) {
            addrFilled++;
        }

        if (await fillSelectField(['#Field-countryInput','select[name="billingCountry"]','select[name="country"]','select[autocomplete="country"]'], "''' + country + '''", countryTextMap["''' + country + '''"])) {
            addrFilled++;
            await waitForStateOptions();
        }

        const stateVal = "''' + state + '''";
        if (stateVal) {
            const stateSelectors = [
                '#Field-administrativeAreaInput',
                'select[id$="-administrativeAreaInput"]',
                'select[name="billingAdministrativeArea"]',
                'select[name="administrativeArea"]',
                'select[name="state"]',
                'select[autocomplete="address-level1"]',
                'select[autocomplete~="address-level1"]'
            ];
            const stateEl = find(stateSelectors);
            if (stateEl) {
                if (stateEl.tagName === 'SELECT') {
                    if (await fillSelectField(stateSelectors, stateVal, stateVal, {skipIfFilled: false})) {
                        addrFilled++;
                    }
                } else if (await fillTextField(stateSelectors, stateVal, {skipIfFilled: false})) {
                    addrFilled++;
                }
            }
        }

        if (await fillTextField(['#Field-localityInput','input[name="billingLocality"]','input[name="locality"]','input[name="city"]','input[autocomplete="address-level2"]'], "''' + city + '''", {skipIfFilled: false})) {
            addrFilled++;
        }

        if (await fillTextField(['#Field-postalCodeInput','input[name="billingPostalCode"]','input[name="postalCode"]','input[name="postal_code"]','input[autocomplete="postal-code"]'], "''' + zip_code + '''", {skipIfFilled: false})) {
            addrFilled++;
        }

        if (await fillTextField(['#Field-addressLine1Input','input[name="billingAddressLine1"]','input[name="addressLine1"]','input[name="address"]','input[autocomplete="address-line1"]'], "''' + address + '''", {skipIfFilled: false})) {
            addrFilled++;
        }

        if (await fillTextField(['#Field-addressLine2Input','input[name="billingAddressLine2"]','input[name="addressLine2"]','input[autocomplete="address-line2"]'], "''' + address_line2 + '''", {skipIfFilled: false})) {
            addrFilled++;
        }
    }

    await prepareCheckoutForm();

    // ======== 卡片字段 ========
    const cardNumSel = ['#Field-numberInput','input[name="number"]','input[name=cardNumber]','input[autocomplete="cc-number"]','input[data-elements-stable-field-name="cardNumber"]'];
    const expirySel  = ['#Field-expiryInput','#Field-expiry','#Field-cardExpiry','input[name="expiry"]','input[name="cardExpiry"]','input[autocomplete="cc-exp"]','input[data-elements-stable-field-name="cardExpiry"]','input[placeholder*="MM"]'];
    const cvcSel     = ['#Field-cvcInput','#Field-cvc','input[name="cvc"]','input[name="cardCvc"]','input[autocomplete="cc-csc"]','input[data-elements-stable-field-name="cardCvc"]'];

    if (shouldFillCard) {
        const cardNum = find(cardNumSel);
        if (cardNum) { typeChars(cardNum, "''' + card_number + '''"); cardFilled++; await delay(80); }
        const expiry = find(expirySel);
        if (expiry) { typeChars(expiry, "''' + expiry + '''"); cardFilled++; await delay(80); }
        const cvc = find(cvcSel);
        if (cvc) { typeChars(cvc, "''' + cvv + '''"); cardFilled++; await delay(80); }
    }

    if (shouldFillBilling) {
        await fillNonCardFields();
    }

    return {cardFilled: cardFilled, addrFilled: addrFilled, total: cardFilled + addrFilled};
})();
'''


def fill_plus_form(page, card_info: dict, email: str = "", fill_mode: str = "all", auto_submit: bool = True) -> dict:
    """填充支付表单 — JS 注入版（参考 AI填卡助手）"""
    step = "FillForm"
    normalized_fill_mode = _normalize_fill_mode(fill_mode)
    logger.log(step, "========== 开始填充支付表单（JS注入模式）==========", "INFO")
    logger.log(step, f"当前页面 URL: {page.url}", "INFO")

    total_filled = 0

    # 构建 JS 填充脚本
    main_fill_mode, iframe_fill_mode = _resolve_fill_context_modes(normalized_fill_mode)
    main_fill_js = _build_fill_js(card_info, email, fill_mode=main_fill_mode)
    iframe_fill_js = _build_fill_js(card_info, email, fill_mode=iframe_fill_mode)
    fallback_main_card_fill_js = (
        _build_fill_js(card_info, email, fill_mode="card_only")
        if normalized_fill_mode == "all" else None
    )

    # 先在主页面执行一次，确保卡支付模式/手动地址模式被打开，并填充主页面账单字段
    logger.log(step, "先在主页面执行准备与账单回填脚本...", "INFO")
    try:
        result = page.run_js(main_fill_js)
        if result and isinstance(result, dict):
            main_filled = result.get('total', 0)
            total_filled += main_filled
            logger.log(step, f"主页面: 填充 {main_filled} 个字段", "SUCCESS" if main_filled > 0 else "WARN")
    except Exception as e:
        logger.log(step, f"主页面准备/回填失败: {e}", "WARN")

    # 查找 iframe
    logger.log(step, "查找支付表单 iframe...", "INFO")
    payment_iframes = []

    for retry in range(5):
        iframes = page.eles('tag:iframe')
        for iframe in iframes:
            src = (iframe.attr('src') or '').lower()
            name = (iframe.attr('name') or '').lower()
            title = (iframe.attr('title') or '').lower()
            payment_keywords = ['payment', 'card', 'stripe', 'elements-inner', 'payment-element', 'card-element']
            if any(kw in src or kw in name or kw in title for kw in payment_keywords):
                if iframe not in payment_iframes:
                    payment_iframes.append(iframe)
                    logger.log(step, f"✓ 找到支付 iframe #{len(payment_iframes)}: {src[:60]}...", "SUCCESS")
        if payment_iframes or retry >= 3:
            break
        time.sleep(1)

    if payment_iframes:
        logger.log(step, f"向 {len(payment_iframes)} 个 iframe 注入填充脚本...", "INFO")
        for idx, iframe in enumerate(payment_iframes):
            src = (iframe.attr('src') or '')[:60]
            try:
                pf = page.get_frame(iframe)
                time.sleep(0.5)

                # 先检测 iframe 内是否有卡号输入框（确认 JS 能执行）
                has_card_field = False
                try:
                    has_card_field = pf.run_js(
                        "return !!document.querySelector('#Field-numberInput,input[name=\"number\"],input[autocomplete=\"cc-number\"]')"
                    )
                except Exception:
                    pass

                result = pf.run_js(iframe_fill_js)
                if result and isinstance(result, dict):
                    filled = result.get('total', 0)
                    total_filled += filled
                    logger.log(step, f"   iframe #{idx+1}: 填充 {filled} 个字段 (卡:{result.get('cardFilled',0)} 地址:{result.get('addrFilled',0)}) [{src}]", "SUCCESS" if filled > 0 else "WARN")
                else:
                    # run_js 返回 None — async IIFE 的 Promise 无法被 DrissionPage 捕获
                    # 但 JS 可能实际执行了（typeChars 逐字符输入）
                    if has_card_field:
                        logger.log(step, f"   iframe #{idx+1}: 有卡号字段，JS 已执行（返回值无法捕获）[{src}]", "INFO")
                        total_filled += 3  # 假设卡号/有效期/CVV 已填
                    else:
                        logger.log(step, f"   iframe #{idx+1}: 无卡号字段 [{src}]", "WARN")
            except Exception as e:
                logger.log(step, f"   iframe #{idx+1} 注入失败: {e} [{src}]", "WARN")
    elif fallback_main_card_fill_js:
        logger.log(step, "未找到支付 iframe，回退到主页面补卡字段...", "INFO")
        try:
            result = page.run_js(fallback_main_card_fill_js)
            if result and isinstance(result, dict):
                filled = result.get('total', 0)
                total_filled += filled
                logger.log(step, f"主页面回退卡片回填: {filled} 个字段", "SUCCESS" if filled > 0 else "WARN")
        except Exception as e:
            logger.log(step, f"主页面回退卡片回填失败: {e}", "WARN")

    # 补充：主页面邮编（可能在 iframe 外）
    if total_filled > 0 and normalized_fill_mode != "card_only":
        try:
            main_zip_js = '''
            (function() {
                const sels = ['#billingAddress-postalCodeInput','input[name="billingPostalCode"]','input[name="postalCode"]','input[autocomplete="billing postal-code"]'];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && !(el.value||'').trim()) {
                        const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
                        if(ns) ns.call(el,"''' + card_info.get('zip_code', '') + '''");
                        const t=el._valueTracker; if(t) t.setValue('');
                        el.dispatchEvent(new Event('input',{bubbles:true}));
                        el.dispatchEvent(new Event('change',{bubbles:true}));
                        return true;
                    }
                }
                return false;
            })();
            '''
            if page.run_js(main_zip_js):
                total_filled += 1
                logger.log(step, "   ✓ 主页面邮编补充填充", "SUCCESS")
        except:
            pass

    if normalized_fill_mode == "all":
        fill_success = total_filled >= 6
    else:
        fill_success = total_filled > 0
    logger.log(step, f"========== 填充完成: {total_filled} 个字段 ==========",
               "SUCCESS" if fill_success else "WARN")

    submit_clicked = False
    if fill_success and auto_submit and normalized_fill_mode == "all":
        logger.log(step, "回填判定成功，开始自动点击订阅按钮...", "INFO")
        for attempt in range(1, 4):
            wait_seconds = 5 if attempt == 1 else 1
            logger.log(step, f"等待 {wait_seconds} 秒后尝试自动点击订阅按钮 (第 {attempt}/3 次)...", "INFO")
            time.sleep(wait_seconds)
            subscribe_result = click_submit_button(page)
            if subscribe_result.get('success'):
                submit_clicked = True
                logger.log(step, "✓ 已自动点击订阅按钮", "SUCCESS")
                break
            logger.log(step, f"⚠ 第 {attempt}/3 次自动点击订阅按钮失败", "WARN")
        if not submit_clicked:
            logger.log(step, "⚠ 未能自动点击订阅按钮，请手动点击", "WARN")

    return {"success": fill_success, "filled_count": total_filled, "submit_clicked": submit_clicked}


def click_submit_button(page) -> dict:
    """点击提交/订阅按钮"""
    step = "Submit"

    # 优先使用内联 Stripe Checkout confirm（绕过 431 模式）
    try:
        has_inline = page.run_js("return !!window._stripeCheckout")
        if has_inline:
            logger.log(step, "检测到内联 Stripe Checkout，使用 confirm() 提交...", "INFO")
            result = stripe_inline_confirm(page)
            if result.get("success"):
                return {"success": True}
            else:
                error_msg = result.get("error", "")
                logger.log(step, f"内联 confirm 返回: {error_msg}", "WARN")
                # 不 return，尝试下面的按钮点击作为回退
    except Exception as e:
        logger.log(step, f"内联 confirm 检测异常: {e}", "WARN")

    # 优先级从高到低的选择器
    selectors = [
        # ChatGPT 新版订阅按钮
        'button.btn-primary[type="submit"]',
        'button[aria-label="订阅"]',
        'button:has-text("订阅")',

        # Stripe 标准按钮
        'button.SubmitButton',
        'button[data-testid="hosted-payment-submit-button"]',

        # 通用选择器
        'button[type="submit"]',
        'button:contains("Subscribe")',
        'button:contains("订阅")',
        'button:contains("提交")',
    ]

    for selector in selectors:
        try:
            # 尝试 CSS 选择器
            btn = page.ele(f'css:{selector}', timeout=1)
            if btn:
                logger.log(step, f"找到按钮（选择器: {selector}）", "INFO")
                btn.click()
                logger.log(step, "✓ 已点击订阅按钮", "SUCCESS")
                return {"success": True}
        except:
            pass

    logger.log(step, "未找到订阅按钮", "WARN")
    return {"success": False}


def check_payment_success(page) -> bool:
    """检测支付是否成功"""
    url = page.url.lower()
    if 'chatgpt.com/payments/success-team' in url:
        return True
    if 'chatgpt.com' in url and 'pay' not in url and 'checkout' not in url and 'auth' not in url:
        # 内联模式下仍在 chatgpt.com，不能仅靠 URL 判断
        # 需要额外检查是否有 stripe-inline-root 存在（说明是内联模式）
        try:
            has_inline = page.run_js("return !!document.getElementById('stripe-inline-root')")
            if has_inline:
                # 内联模式：检查 confirm 结果
                confirm_result = page.run_js("return window._stripeConfirmResult")
                if confirm_result and confirm_result.get("success"):
                    return True
                return False  # 还在内联模式中，不算成功
        except Exception:
            pass
        return True
    if any(k in url for k in ['success', 'thank', 'confirmed', 'complete']):
        return True
    return False


# ==================== 表单状态检测 ====================
_FORM_STATUS_JS = '''
(function() {
    var result = {
        stripeReady: false,
        iframeCount: 0,
        submitReady: false,
        submitText: '',
        hasError: false,
        errorMessage: '',
        pageUrl: location.href,
    };

    // Stripe iframe 检测
    var iframes = document.querySelectorAll('iframe');
    var stripeCount = 0;
    for (var i = 0; i < iframes.length; i++) {
        var src = (iframes[i].src || '').toLowerCase();
        if (src.indexOf('stripe') >= 0 || src.indexOf('payment') >= 0) {
            stripeCount++;
        }
    }
    result.iframeCount = stripeCount;
    result.stripeReady = stripeCount > 0;

    // 提交按钮检测
    var btnSels = [
        'button.SubmitButton',
        'button[data-testid="hosted-payment-submit-button"]',
        'button[type="submit"]:not([id^="btn-"])',
    ];
    for (var j = 0; j < btnSels.length; j++) {
        var btn = document.querySelector(btnSels[j]);
        if (btn && btn.offsetWidth > 0) {
            result.submitReady = !btn.disabled;
            result.submitText = (btn.textContent || '').trim().substring(0, 50);
            break;
        }
    }

    // 错误消息检测（卡拒绝等）
    var errSels = [
        '[role="alert"]',
        '.StripeError',
        '.Error--message',
        '[data-testid="card-errors"]',
        '.p-FieldError',
        '.CardErrors',
    ];
    for (var k = 0; k < errSels.length; k++) {
        var el = document.querySelector(errSels[k]);
        if (el && el.textContent && el.textContent.trim()) {
            result.hasError = true;
            result.errorMessage = el.textContent.trim().substring(0, 200);
            break;
        }
    }

    // 也在 iframe 内部找错误
    if (!result.hasError) {
        for (var fi = 0; fi < iframes.length; fi++) {
            try {
                var doc = iframes[fi].contentDocument;
                if (!doc) continue;
                for (var ek = 0; ek < errSels.length; ek++) {
                    var iel = doc.querySelector(errSels[ek]);
                    if (iel && iel.textContent && iel.textContent.trim()) {
                        result.hasError = true;
                        result.errorMessage = iel.textContent.trim().substring(0, 200);
                        break;
                    }
                }
                if (result.hasError) break;
            } catch(e) {}
        }
    }

    return result;
})();
'''


def _check_form_status(page) -> dict:
    """检测支付表单的当前状态（Stripe 是否就绪、是否有错误等）"""
    result = {"hasError": False}
    try:
        iframes = page.eles('tag:iframe')
        stripe_count = 0
        for iframe in iframes:
            src = (iframe.attr('src') or '').lower()
            if 'stripe' in src or 'payment' in src:
                stripe_count += 1
        result["iframeCount"] = stripe_count
        result["stripeReady"] = stripe_count > 0

        # 检查提交按钮
        try:
            btn = page.ele('css:button[type="submit"]', timeout=0.5)
            if btn:
                result["submitReady"] = True
                result["submitText"] = (btn.text or "")[:50]
        except Exception:
            result["submitReady"] = False

        # 检查主页面错误消息
        try:
            alert = page.ele('css:[role="alert"]', timeout=0.3)
            if alert and alert.text:
                result["hasError"] = True
                result["errorMessage"] = alert.text[:200]
        except Exception:
            pass

        # 检查 Stripe iframe 内的错误消息（"您的银行卡被拒绝了" 等）
        if not result["hasError"]:
            for iframe in iframes:
                src = (iframe.attr('src') or '').lower()
                if 'elements-inner' not in src:
                    continue
                try:
                    pf = page.get_frame(iframe)
                    # 用简单的 querySelector 检查错误
                    err_text = pf.run_js('''
                        var sels = ['[role="alert"]','.Error','.p-FieldError','.CardErrors'];
                        for (var i=0; i<sels.length; i++) {
                            var el = document.querySelector(sels[i]);
                            if (el && el.textContent && el.textContent.trim()) return el.textContent.trim();
                        }
                        return '';
                    ''')
                    if err_text and isinstance(err_text, str) and len(err_text) > 2:
                        result["hasError"] = True
                        result["errorMessage"] = err_text[:200]
                        break
                except Exception:
                    pass

    except Exception as e:
        result["_error"] = str(e)[:100]

    return result

    return result


def _simulate_human_behavior(page, extra_wait: float = 3.0) -> None:
    """模拟真人行为让 Stripe Radar 收集足够的行为数据。"""
    try:
        page.run_js('''
        (function() {
            var w = window.innerWidth || 1440, h = window.innerHeight || 900;
            for (var i = 0; i < 10; i++) {
                var x = Math.floor(Math.random() * w), y = Math.floor(Math.random() * h);
                window.dispatchEvent(new MouseEvent('mousemove', {clientX: x, clientY: y, bubbles: true}));
                document.dispatchEvent(new MouseEvent('mousemove', {clientX: x, clientY: y, bubbles: true}));
            }
            for (var j = 0; j < 2; j++) {
                window.dispatchEvent(new WheelEvent('wheel', {deltaY: Math.random() * 100 - 50, bubbles: true}));
            }
            window.dispatchEvent(new FocusEvent('focus'));
        })();
        ''')
    except Exception:
        pass
    time.sleep(extra_wait)


# ==================== 注入控制按钮 ====================

# Stripe iframe 网络拦截器 JS — 注入到 Stripe iframe 内部捕获 confirm 请求/响应
_STRIPE_IFRAME_INTERCEPTOR_JS = '''
(function() {
    if (window.__stripeInterceptorInstalled) return 'already_installed';
    window.__stripeInterceptorInstalled = true;
    window.__stripeCapturedRequests = [];

    const _origFetch = window.fetch;
    window.fetch = async function(...args) {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        const method = (args[1] && args[1].method) || 'GET';
        const reqBody = (args[1] && args[1].body) || null;

        const isKey = url.includes('api.stripe.com') || url.includes('m.stripe.com') || url.includes('r.stripe.com');
        if (!isKey) return _origFetch.apply(this, args);

        const entry = {
            url: url.substring(0, 300),
            method: method,
            reqBodyLen: reqBody ? reqBody.length || 0 : 0,
            reqBodyPreview: typeof reqBody === 'string' ? reqBody.substring(0, 3000) : null,
            ts: Date.now(),
        };

        try {
            const resp = await _origFetch.apply(this, args);
            const clone = resp.clone();
            entry.status = resp.status;
            try {
                const text = await clone.text();
                entry.respLen = text.length;
                entry.respPreview = text.substring(0, 3000);
            } catch(e) { entry.respErr = e.message; }
            window.__stripeCapturedRequests.push(entry);
            return resp;
        } catch(e) {
            entry.fetchErr = e.message;
            window.__stripeCapturedRequests.push(entry);
            throw e;
        }
    };
    return 'installed';
})();
'''


def inject_stripe_interceptor(page) -> int:
    """向页面中所有 Stripe iframe 注入 fetch 拦截器，捕获 confirm 请求/响应。"""
    step = "Intercept"
    injected = 0

    try:
        iframes = page.eles('tag:iframe')
    except Exception:
        return 0

    for iframe in iframes:
        src = (iframe.attr('src') or '').lower()
        name = (iframe.attr('name') or '').lower()
        title = (iframe.attr('title') or '').lower()
        keywords = ['payment', 'card', 'stripe', 'elements-inner', 'checkout']
        if not any(kw in src or kw in name or kw in title for kw in keywords):
            continue
        try:
            pf = page.get_frame(iframe)
            result = pf.run_js(_STRIPE_IFRAME_INTERCEPTOR_JS)
            if result == 'installed':
                injected += 1
                logger.log(step, f"✓ 拦截器已注入 iframe: {src[:60]}", "SUCCESS")
            elif result == 'already_installed':
                injected += 1
        except Exception as e:
            logger.log(step, f"注入 iframe 拦截器失败: {e}", "WARN")

    # 也在主页面注入（有时 Stripe 元素不在 iframe 里）
    try:
        result = page.run_js(_STRIPE_IFRAME_INTERCEPTOR_JS)
        if result == 'installed':
            injected += 1
    except Exception:
        pass

    return injected


def collect_stripe_captured_requests(page) -> list:
    """从所有 iframe + 主页面收集拦截到的 Stripe 请求数据。"""
    all_captured = []

    # 主页面
    try:
        main_data = page.run_js("return window.__stripeCapturedRequests || []")
        if main_data:
            for entry in main_data:
                entry["source"] = "main"
            all_captured.extend(main_data)
    except Exception:
        pass

    # iframe
    try:
        iframes = page.eles('tag:iframe')
        for idx, iframe in enumerate(iframes):
            src = (iframe.attr('src') or '').lower()
            if not any(kw in src for kw in ['stripe', 'payment', 'elements']):
                continue
            try:
                pf = page.get_frame(iframe)
                data = pf.run_js("return window.__stripeCapturedRequests || []")
                if data:
                    for entry in data:
                        entry["source"] = f"iframe_{idx}"
                    all_captured.extend(data)
            except Exception:
                pass
    except Exception:
        pass

    return all_captured


def inject_fill_button(page, card_info: dict):
    """注入填充按钮到页面（居中，深色半透明背景）"""
    selected_country = normalize_billing_country(card_info.get('country', 'KR'))
    country_options_html = ''.join(
        f'<option value="{code}"{" selected" if code == selected_country else ""}>{label}</option>'
        for code, label in BILLING_COUNTRY_LABELS.items()
    )
    js_code = f'''
    (function() {{
        if (document.getElementById('card-fill-container')) return;

        const container = document.createElement('div');
        container.id = 'card-fill-container';
        container.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:99999;background:rgba(30,30,30,0.95);padding:20px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.5);font-family:Arial;color:#fff;min-width:320px;';

        container.innerHTML = `
            <div style="margin-bottom:12px;font-weight:bold;font-size:16px;color:#4CAF50;">💳 卡片信息</div>
            <div id="card-display" style="font-size:13px;margin-bottom:15px;line-height:1.6;color:#e0e0e0;">
                <div><strong>卡号:</strong> {card_info['card_number_formatted']}</div>
                <div><strong>有效期:</strong> {card_info['expiry_date']}</div>
                <div><strong>CVV:</strong> {card_info['cvv']}</div>
                <div><strong>姓名:</strong> {card_info.get('full_name', '')}</div>
            </div>
            <div style="display:flex;gap:10px;align-items:flex-end;margin-bottom:12px;">
                <div style="flex:1;">
                    <label for="card-bin-input" style="display:block;margin-bottom:6px;font-size:12px;color:#bdbdbd;">卡头</label>
                    <input
                        id="card-bin-input"
                        type="text"
                        inputmode="numeric"
                        pattern="[0-9]*"
                        maxlength="8"
                        value="{card_info.get('bin_prefix', '625003')}"
                        placeholder="留空则使用当前卡头"
                        style="width:100%;box-sizing:border-box;padding:8px 10px;border-radius:6px;border:1px solid #555;background:#1f1f1f;color:#fff;outline:none;"
                    >
                </div>
                <div style="width:120px;">
                    <label for="country-select" style="display:block;margin-bottom:6px;font-size:12px;color:#bdbdbd;">国家</label>
                    <select
                        id="country-select"
                        style="width:100%;box-sizing:border-box;padding:8px 10px;border-radius:6px;border:1px solid #555;background:#1f1f1f;color:#fff;outline:none;"
                    >{country_options_html}</select>
                </div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
                <button id="btn-fill" style="flex:1;padding:10px 15px;cursor:pointer;background:#4CAF50;color:white;border:none;border-radius:6px;font-weight:bold;transition:background 0.3s;">📝 填充</button>
                <button id="btn-new-card" style="flex:1;padding:10px 15px;cursor:pointer;background:#2196F3;color:white;border:none;border-radius:6px;font-weight:bold;transition:background 0.3s;">🔄 换卡</button>
                <button id="btn-replace-card-only" style="flex:1;padding:10px 15px;cursor:pointer;background:#1565C0;color:white;border:none;border-radius:6px;font-weight:bold;transition:background 0.3s;">💳 仅换卡</button>
                <button id="btn-replace-billing-only" style="flex:1;padding:10px 15px;cursor:pointer;background:#7B1FA2;color:white;border:none;border-radius:6px;font-weight:bold;transition:background 0.3s;">🏠 仅换地址</button>
            </div>
            <div style="display:flex;gap:8px;">
                <button id="btn-submit" style="flex:1;padding:10px 15px;cursor:pointer;background:#FF9800;color:white;border:none;border-radius:6px;font-weight:bold;transition:background 0.3s;">✅ 提交</button>
            </div>
            <div id="ip-display" style="margin-top:12px;font-size:11px;color:#999;text-align:center;"></div>
        `;

        document.body.appendChild(container);

        // 按钮悬停效果
        document.getElementById('btn-fill').onmouseover = function() {{ this.style.background = '#45a049'; }};
        document.getElementById('btn-fill').onmouseout = function() {{ this.style.background = '#4CAF50'; }};
        document.getElementById('btn-new-card').onmouseover = function() {{ this.style.background = '#1976D2'; }};
        document.getElementById('btn-new-card').onmouseout = function() {{ this.style.background = '#2196F3'; }};
        document.getElementById('btn-replace-card-only').onmouseover = function() {{ this.style.background = '#0D47A1'; }};
        document.getElementById('btn-replace-card-only').onmouseout = function() {{ this.style.background = '#1565C0'; }};
        document.getElementById('btn-replace-billing-only').onmouseover = function() {{ this.style.background = '#6A1B9A'; }};
        document.getElementById('btn-replace-billing-only').onmouseout = function() {{ this.style.background = '#7B1FA2'; }};
        document.getElementById('btn-submit').onmouseover = function() {{ this.style.background = '#F57C00'; }};
        document.getElementById('btn-submit').onmouseout = function() {{ this.style.background = '#FF9800'; }};
        const cardBinInput = document.getElementById('card-bin-input');
        const countrySelect = document.getElementById('country-select');
        const readCardBinValue = function() {{
            if (!cardBinInput) return '';
            const clean = String(cardBinInput.value || '').replace(/\\D+/g, '').slice(0, 8);
            cardBinInput.value = clean;
            return clean;
        }};
        const readCountryValue = function() {{
            if (!countrySelect) return 'SG';
            const value = String(countrySelect.value || 'SG').toUpperCase();
            return ['SG', 'KR', 'US'].includes(value) ? value : 'SG';
        }};
        if (cardBinInput) {{
            cardBinInput.addEventListener('input', function() {{
                readCardBinValue();
            }});
        }}

        document.getElementById('btn-fill').onclick = function() {{
            this.disabled = true;
            this.textContent = '⏳ 填充中...';
            window._cardAction = 'fill';
            window._cardActionBin = '';
            window._cardActionCountry = readCountryValue();
        }};

        document.getElementById('btn-new-card').onclick = function() {{
            window._cardAction = 'new_card';
            window._cardActionBin = readCardBinValue();
            window._cardActionCountry = readCountryValue();
        }};

        document.getElementById('btn-replace-card-only').onclick = function() {{
            window._cardAction = 'replace_card_only';
            window._cardActionBin = readCardBinValue();
            window._cardActionCountry = readCountryValue();
        }};

        document.getElementById('btn-replace-billing-only').onclick = function() {{
            window._cardAction = 'replace_billing_only';
            window._cardActionBin = '';
            window._cardActionCountry = readCountryValue();
        }};

        document.getElementById('btn-submit').onclick = function() {{
            window._cardAction = 'submit';
            window._cardActionBin = '';
            window._cardActionCountry = readCountryValue();
        }};

        window._resetFillButton = function() {{
            const btn = document.getElementById('btn-fill');
            if (btn) {{
                btn.disabled = false;
                btn.textContent = '📝 填充';
            }}
        }};

        window._updateCardDisplay = function(info) {{
            const display = document.getElementById('card-display');
            if (display) {{
                display.innerHTML = `
                    <div><strong>卡号:</strong> ${{info.card_number}}</div>
                    <div><strong>有效期:</strong> ${{info.expiry}}</div>
                    <div><strong>CVV:</strong> ${{info.cvv}}</div>
                    <div><strong>姓名:</strong> ${{info.name}}</div>
                `;
            }}
        }};

        window._updateIpDisplay = function(ip) {{
            const display = document.getElementById('ip-display');
            if (display) {{
                display.textContent = '🌐 出口IP: ' + ip;
            }}
        }};
    }})();
    '''

    try:
        page.run_js(js_code)
        logger.log("Inject", "✓ 已注入控制按钮（居中显示）", "SUCCESS")
    except Exception as e:
        logger.log("Inject", f"注入失败: {e}", "WARN")


def update_card_display(page, card_info: dict):
    """更新卡片显示"""
    try:
        page.run_js(f'''
            if (window._updateCardDisplay) {{
                window._updateCardDisplay({{
                    card_number: "{card_info['card_number_formatted']}",
                    expiry: "{card_info['expiry_date']}",
                    cvv: "{card_info['cvv']}",
                    name: "{card_info.get('full_name', '')}"
                }});
            }}
        ''')
    except:
        pass


# ==================== 主支付流程 ====================
def do_payment(
    cookies,
    email: str = "",
    plan_type: str = "business",
    workspace_name: str = "",
    seat_quantity: int = 5,
    country: str = "SG",
    checkout_country: str = "AUTO",
    currency: str = "USD",
    max_card_retries: int = 5,
    timeout: int = 900,
    use_proxy: bool = False,
    proxy_port: int = None,
    headless: bool = False,
    thread_id: str = None,
    checkout_url: str = "",
    kr_success_mode: str | None = None,
    proxy_url: str = "",
    paypal_profile_key: str = "",
    paypal_profile_bypass_proxy: bool = False,
) -> dict:
    """
    执行支付流程（优化版 - 使用 API 直接调用）

    Args:
        cookies: 登录 cookies
        email: 邮箱
        plan_type: 套餐类型 (plus/business)
        workspace_name: 团队空间名称
        seat_quantity: 座位数量
        country: 账单国家
        currency: 货币
        max_card_retries: 最大重试次数
        timeout: 超时时间
        use_proxy: 是否使用显式代理
        proxy_port: 代理端口
        headless: 无头模式
        thread_id: 线程标识
    """
    step = "Payment"
    page = None
    net_collector = None
    profile_lock_info = None
    profile_dir = ""
    profile_debug_key = ""
    normalized_paypal_profile_key = ""

    try:
        # 生成卡片信息
        logger.log(step, "生成卡片信息...", "INFO")
        logger.log(step, f"地址国家={country} | 结账国家={checkout_country}", "INFO")
        card_info = generate_card_info(country=country, kr_success_mode=kr_success_mode)
        logger.log(step, f"卡号: {card_info['card_number_formatted']}", "INFO")
        logger.log(step, f"姓名: {card_info['full_name']}", "INFO")
        logger.log(step,
                   f"地址: {card_info['address']}, {card_info['city']}, {card_info['state']} {card_info['zip_code']}",
                   "INFO")

        normalized_paypal_profile_key = normalize_paypal_profile_key(paypal_profile_key)
        profile_dir = resolve_payment_browser_profile_dir(
            thread_id=thread_id,
            paypal_profile_key=normalized_paypal_profile_key,
        )
        if normalized_paypal_profile_key:
            profile_lock_info = acquire_payment_profile_lock(profile_dir)
            profile_debug_key = normalized_paypal_profile_key
            logger.log(step, f"启用 PayPal 登录态复用: {normalized_paypal_profile_key}", "INFO")
        elif thread_id:
            profile_debug_key = str(thread_id)

        effective_proxy_url, effective_use_proxy, effective_proxy_port = resolve_effective_proxy_settings(
            proxy_url=proxy_url,
            use_proxy=use_proxy,
            proxy_port=proxy_port,
            paypal_profile_key=normalized_paypal_profile_key,
            paypal_profile_bypass_proxy=paypal_profile_bypass_proxy,
        )
        if paypal_profile_bypass_proxy and normalized_paypal_profile_key:
            logger.log(step, "PayPal 登录态复用已启用直连模式，本次不走代理", "INFO")

        # 启动浏览器
        page = create_browser(
            headless=headless,
            use_proxy=effective_use_proxy,
            proxy_port=effective_proxy_port,
            thread_id=thread_id,
            proxy_url=effective_proxy_url,
            profile_dir=profile_dir,
            profile_debug_key=profile_debug_key,
        )
        if not page:
            return {"success": False, "message": "创建浏览器失败"}

        # 启动网络请求收集器
        if NetTraceCollector is not None:
            try:
                net_collector = NetTraceCollector(
                    page, email=email, thread_id=thread_id or "",
                )
                net_collector.start(
                    card_info=card_info,
                    extra_meta={
                        "plan_type": plan_type,
                        "country": country,
                        "checkout_country": checkout_country,
                        "currency": currency,
                        "checkout_url": checkout_url or "",
                        "use_proxy": effective_use_proxy,
                        "proxy_port": effective_proxy_port,
                        "proxy_url": effective_proxy_url or "",
                    },
                )
                logger.log(step, "✓ 网络请求收集器已启动", "SUCCESS")
            except Exception as exc:
                logger.log(step, f"网络请求收集器启动失败（不影响支付）: {exc}", "WARN")
                net_collector = None

        # 设置 Cookies
        logger.log(step, "设置 Cookies...", "INFO")
        cookie_result = set_cookies(page, cookies, "chatgpt.com")

        if not cookie_result.get("success"):
            logger.log(step, f"Cookies 设置失败: {cookie_result.get('message')}", "ERROR")
            return {
                "success": False,
                "message": cookie_result.get("message", "Cookies 设置失败"),
                "cookie_count": cookie_result.get("count", 0)
            }

        if net_collector:
            net_collector.mark_event("cookies_set", f"count={cookie_result.get('count', 0)}")

        # 优先使用外部传入的 checkout_url；否则回退到内部 API 生成支付页。
        if str(checkout_url or "").strip():
            logger.log(step, "========== 使用外部 checkout_url 跳转支付页面 ==========", "INFO")
            nav_result = navigate_to_checkout_url(page, checkout_url)
        else:
            logger.log(step, "========== 使用 API 直接导航到支付页面（绕过 A/B 测试）==========", "INFO")
            nav_result = navigate_via_api(
                page,
                plan_type=plan_type,
                email=email,
                workspace_name=workspace_name,
                seat_quantity=seat_quantity,
                country=country,
                checkout_country=checkout_country,
                currency=currency
            )

        if net_collector:
            net_collector.mark_event("navigate_result", json.dumps(nav_result, ensure_ascii=False, default=str)[:500])
            net_collector.mark_event("navigate_mode", "inline_checkout" if nav_result.get("inline_checkout") else "page_navigate")

        if not nav_result.get("success"):
            logger.log(step, f"API 导航失败: {nav_result.get('message')}", "ERROR")
            return {"success": False, "message": nav_result.get("message", "导航失败")}

        logger.log(step, f"✓ 成功导航到支付页面: {nav_result.get('payment_url')}", "SUCCESS")

        # 注入控制按钮
        inject_fill_button(page, card_info)

        # 注入 Stripe iframe 拦截器（捕获 confirm 请求/响应 body）
        time.sleep(2)  # 等 Stripe iframe 加载
        interceptor_count = inject_stripe_interceptor(page)
        logger.log(step, f"Stripe 拦截器注入: {interceptor_count} 个", "INFO" if interceptor_count > 0 else "WARN")

        # 进入手动模式循环
        return manual_payment_loop(
            page,
            card_info,
            email,
            max_card_retries,
            timeout,
            plan_type,
            kr_success_mode=kr_success_mode,
            net_collector=net_collector,
        )

    except Exception as e:
        import traceback
        logger.log(step, f"支付异常: {e}", "ERROR")
        traceback.print_exc(file=sys.stderr)
        return {"success": False, "message": str(e)}
    finally:
        # 保存网络 trace（无论成功失败都保存）
        if net_collector:
            try:
                # 这里还不知道最终结果，由 manual_payment_loop 已经保存过了
                # 如果是异常退出（未走到 loop），则在此兜底保存
                if net_collector._running:
                    try:
                        _captured = collect_stripe_captured_requests(page)
                        if _captured:
                            net_collector.mark_event("stripe_captured", json.dumps(_captured, ensure_ascii=False, default=str))
                    except Exception:
                        pass
                    trace_path = net_collector.stop_and_save(success=False, result_detail="exception_exit")
                    if trace_path:
                        logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
            except Exception:
                pass
        if page:
            browser_pid = None
            try:
                browser_pid = page.browser.process_id
            except Exception:
                browser_pid = None
            close_payment_browser(
                page,
                profile_dir=profile_dir,
                browser_pid=browser_pid,
                thread_id=thread_id,
                preserve_profile_session=bool(normalized_paypal_profile_key),
            )
        release_payment_profile_lock(profile_lock_info)


_AUTO_PAYMENT_DEFAULTS = {
    "fill_wait": [8, 15],        # Stripe 加载后等多久开始填充（秒）
    "card_to_addr_wait": [3, 8], # 填卡号后等多久填地址（秒）
    "submit_wait": [2, 5],       # 填充完到提交的等待（秒）
    "retry_wait": [8, 15],       # 卡被拒后等多久换卡（秒）
    "max_retries": 5,            # 最大换卡次数
    "behavior_wait": [2, 5],     # 行为模拟持续时间（秒）
}


def _load_auto_payment_config() -> dict:
    """从环境变量加载自动支付参数配置。"""
    raw = os.environ.get("AUTO_PAYMENT_CONFIG", "")
    config = dict(_AUTO_PAYMENT_DEFAULTS)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k in config:
                        config[k] = v
        except Exception:
            pass
    return config


def _rand_delay(range_or_val) -> float:
    """从 [min, max] 区间取随机值，或直接返回固定值。"""
    if isinstance(range_or_val, (list, tuple)) and len(range_or_val) >= 2:
        return random.uniform(float(range_or_val[0]), float(range_or_val[1]))
    return float(range_or_val) if range_or_val else 3.0


def _auto_sleep(label: str, range_or_val, step: str = "Manual") -> float:
    """随机等待并打印日志。"""
    delay = _rand_delay(range_or_val)
    logger.log(step, f"[自动] ⏳ {label} ({delay:.1f}s)", "INFO")
    time.sleep(delay)
    return delay


def manual_payment_loop(
    page,
    card_info: dict,
    email: str,
    max_retries: int,
    timeout: int,
    plan_type: str = "plus",
    kr_success_mode: str | None = None,
    net_collector=None,
) -> dict:
    """支付循环（含自动驾驶逻辑，随机化延迟模拟真人）"""
    step = "Manual"

    # 加载自动支付配置
    _acfg = _load_auto_payment_config()
    _max_retries = int(_acfg.get("max_retries", 5))
    logger.log(step, f"========== 支付循环（自动模式）==========", "INFO")
    logger.log(step, f"配置: fill_wait={_acfg['fill_wait']} card_to_addr={_acfg['card_to_addr_wait']} "
               f"submit={_acfg['submit_wait']} retry={_acfg['retry_wait']} max={_max_retries}", "INFO")

    start_time = time.time()
    last_signal_ts = 0

    # ---- 自动驾驶状态 ----
    _auto_fill_attempts = 0       # 自动填充尝试次数
    _auto_submit_attempts = 0     # 自动提交尝试次数
    _auto_card_changes = 0        # 自动换卡次数
    _last_auto_action_time = 0    # 上次自动操作时间
    _stripe_ready_time = 0        # Stripe 表单就绪时间
    _auto_phase = "wait_stripe"   # 自动阶段: wait_stripe → fill → verify → submit → wait_result

    while time.time() - start_time < timeout:
        try:
            # 检测支付成功
            if check_payment_success(page):
                logger.log(step, "🎉 支付成功!", "SUCCESS")
                card_digits = str(
                    card_info.get('card_number')
                    or card_info.get('card_number_formatted')
                    or ''
                )
                card_digits = re.sub(r"\D+", "", card_digits)
                result = {
                    "success": True,
                    "message": "支付成功",
                    "card_info": {
                        "card_number": card_info['card_number_formatted'],
                        "card_last4": card_digits[-4:],
                        "expiry_date": card_info['expiry_date'],
                        "cvv": card_info['cvv'],
                        "bin_prefix": card_info.get('bin_prefix', '') or card_digits[:6],
                        "full_name": card_info.get('full_name', ''),
                        "country": card_info.get('country', ''),
                        "address": card_info.get('address', ''),
                        "address_line2": card_info.get('address_line2', ''),
                        "city": card_info.get('city', ''),
                        "state": card_info.get('state', ''),
                        "zip_code": card_info.get('zip_code', '')
                    }
                }
                if net_collector:
                    net_collector.mark_event("payment_success")
                    trace_path = net_collector.stop_and_save(success=True, result_detail="支付成功")
                    if trace_path:
                        logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
                        result["net_trace_file"] = trace_path
                return result

            # 检查按钮是否存在（不阻塞手动信号处理）
            try:
                btn_exists = page.run_js("return !!document.getElementById('card-fill-container')")
            except Exception:
                btn_exists = None
            if btn_exists is False:
                inject_fill_button(page, card_info)
                time.sleep(1)

            hcaptcha_result = handle_hcaptcha_challenge(
                page=page,
                step=step,
                card_info=card_info,
                net_collector=net_collector,
            )
            if hcaptcha_result:
                if hcaptcha_result.get("status") == "continue":
                    time.sleep(3)
                    continue
                return hcaptcha_result["result"]

            # ================================================================
            # 手动信号处理（兼容手动操作，优先级高于自动驾驶）
            # 即使 --no-auto-fill（手动模式）也必须保留这些动作：
            # 回填 / 换卡 / 只换卡 / 只换地址 / 提交 / 关闭浏览器
            # ================================================================
            manual_action_processed = False
            global_action, signal_ts, signal_card_bin = read_signal()
            if global_action and signal_ts > last_signal_ts:
                last_signal_ts = signal_ts
                logger.log(step, f"📡 收到全局信号: {global_action}", "INFO")
                page.run_js(
                    f"window._cardAction = {json.dumps(global_action)}; "
                    f"window._cardActionBin = {json.dumps(signal_card_bin or '')}"
                )

            # 检测按钮点击
            action = page.run_js("return window._cardAction")
            if action:
                manual_action_processed = True
                page.run_js("window._cardAction = null")
                action_card_bin = page.run_js("return window._cardActionBin || ''")
                page.run_js("window._cardActionBin = ''")
                action_country = page.run_js(
                    "return window._cardActionCountry || "
                    "(document.getElementById('country-select') ? document.getElementById('country-select').value : '') || ''"
                )
                page.run_js("window._cardActionCountry = ''")
                target_country = normalize_billing_country(action_country or card_info.get('country', 'SG'))

                if action == 'close_connections':
                    logger.log(step, "🛑 收到关闭支付浏览器指令", "WARN")
                    if net_collector:
                        net_collector.mark_event("close_connections")
                        trace_path = net_collector.stop_and_save(success=False, result_detail="手动关闭")
                        if trace_path:
                            logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
                    return {"success": False, "message": "手动关闭支付浏览器，任务已终止", "card_info": card_info}

                if action == 'fill':
                    if target_country != normalize_billing_country(card_info.get('country', 'SG')):
                        card_info = rebuild_card_info_for_country(card_info, target_country, kr_success_mode=kr_success_mode)
                        update_card_display(page, card_info)
                        if net_collector:
                            net_collector.update_card_info(card_info)
                    logger.log(step, "📝 填充表单...", "INFO")
                    if net_collector:
                        net_collector.mark_event("fill_start", f"country={target_country}")
                    result = fill_plus_form(page, card_info, email)
                    if net_collector:
                        net_collector.mark_event("fill_done", f"filled={result.get('filled_count', 0)} submit={result.get('submit_clicked', False)}")
                    try:
                        page.run_js("window._resetFillButton()")
                    except:
                        pass

                elif action == 'new_card':
                    logger.log(step, "🔄 生成新卡号...", "INFO")
                    requested_bin = normalize_card_bin(action_card_bin) or card_info.get('bin_prefix', '625003')
                    card_info = generate_card_info(
                        bin_prefix=requested_bin,
                        country=target_country,
                        kr_success_mode=kr_success_mode,
                    )
                    logger.log(step, f"新卡号: {card_info['card_number_formatted']}", "SUCCESS")
                    update_card_display(page, card_info)
                    if net_collector:
                        net_collector.update_card_info(card_info)

                elif action == 'replace_card_only':
                    logger.log(step, "💳 仅更换卡片信息...", "INFO")
                    requested_bin = normalize_card_bin(action_card_bin) or card_info.get('bin_prefix', '625003')
                    card_info = rebuild_card_info_for_new_card(card_info, requested_bin)
                    update_card_display(page, card_info)
                    result = fill_plus_form(page, card_info, email, fill_mode="card_only", auto_submit=False)
                    logger.log(step, f"仅换卡结果: {result}", "SUCCESS" if result.get('success') else "WARN")

                elif action == 'replace_billing_only':
                    logger.log(step, "🏠 仅更换账单地址信息...", "INFO")
                    card_info = rebuild_card_info_for_country(card_info, target_country, kr_success_mode=kr_success_mode)
                    update_card_display(page, card_info)
                    result = fill_plus_form(page, card_info, email, fill_mode="billing_only", auto_submit=False)
                    logger.log(step, f"仅换地址结果: {result}", "SUCCESS" if result.get('success') else "WARN")

                elif action == 'submit':
                    logger.log(step, "✅ 提交表单...", "INFO")
                    if net_collector:
                        net_collector.mark_event("submit_click")
                    click_submit_button(page)

                elif action == 'check_ip':
                    logger.log(step, "🌐 检测出口IP...", "INFO")
                    try:
                        ip_response = requests.get('https://api.ipify.org?format=json', timeout=10)
                        if ip_response.status_code == 200:
                            ip = ip_response.json().get('ip', 'Unknown')
                            logger.log(step, f"🌐 当前出口IP: {ip}", "SUCCESS")
                            page.run_js(f"window._updateIpDisplay('{ip}')")
                    except Exception as e:
                        logger.log(step, f"IP检测失败: {e}", "WARN")

                if manual_action_processed:
                    time.sleep(0.5)
                    continue

            # ================================================================
            # 自动驾驶逻辑（可通过 --no-auto-fill 关闭）
            # ================================================================
            if os.environ.get("NO_AUTO_FILL") == "1":
                time.sleep(0.5)
                continue

            elapsed = time.time() - start_time
            since_last_auto = time.time() - _last_auto_action_time

            if _auto_phase == "wait_stripe":
                # 阶段1：等待 Stripe 表单加载完成
                if since_last_auto >= 3:
                    _last_auto_action_time = time.time()
                    form_status = _check_form_status(page)
                    stripe_ready = form_status.get("stripeReady", False)
                    iframe_count = form_status.get("iframeCount", 0)

                    if "_error" in form_status:
                        logger.log(step, f"[自动] 表单检测: {form_status}", "WARN")
                    elif not stripe_ready:
                        logger.log(step, f"[自动] 等待 Stripe 加载... (iframe={iframe_count}, {int(elapsed)}s)", "INFO")

                    if stripe_ready:
                        if _stripe_ready_time == 0:
                            _stripe_ready_time = time.time()
                            logger.log(step, f"[自动] ✓ Stripe 表单就绪 ({iframe_count} iframe)", "SUCCESS")
                            _auto_sleep("等待 Stripe 稳定", _acfg["fill_wait"], step)
                            _auto_phase = "fill"
                            logger.log(step, "[自动] → 进入填充阶段", "INFO")

            elif _auto_phase == "fill":
                # 阶段2：自动填充（先卡号 → 随机等待 → 再地址）
                if _auto_fill_attempts < 3 and since_last_auto >= 2:
                    _auto_fill_attempts += 1
                    _last_auto_action_time = time.time()
                    logger.log(step, f"[自动] 🤖 填充 #{_auto_fill_attempts}/3...", "INFO")
                    if net_collector:
                        net_collector.mark_event("auto_fill", f"attempt={_auto_fill_attempts}")

                    # 步骤1: 填卡号
                    logger.log(step, "[自动]   步骤1: 填充卡号...", "INFO")
                    fill_plus_form(page, card_info, email, fill_mode="card_only", auto_submit=False)

                    # 步骤2: 随机等待（让 Stripe 验证卡号并展开地址表单）
                    _auto_sleep("卡号填充后等待", _acfg["card_to_addr_wait"], step)

                    # 步骤3: 填地址
                    logger.log(step, "[自动]   步骤2: 填充账单地址...", "INFO")
                    fill_plus_form(page, card_info, email, fill_mode="billing_only", auto_submit=False)

                    _auto_phase = "verify"
                    logger.log(step, "[自动] → 进入验证阶段", "INFO")

            elif _auto_phase == "verify":
                # 阶段3：行为模拟
                if since_last_auto >= 2:
                    _last_auto_action_time = time.time()
                    behavior_time = _rand_delay(_acfg["behavior_wait"])
                    _simulate_human_behavior(page, extra_wait=behavior_time)
                    _auto_phase = "submit"
                    logger.log(step, "[自动] → 进入提交阶段", "INFO")

            elif _auto_phase == "submit":
                # 阶段4：提交
                if since_last_auto >= 1:
                    _auto_submit_attempts += 1
                    _auto_sleep("提交前等待", _acfg["submit_wait"], step)
                    _last_auto_action_time = time.time()
                    logger.log(step, f"[自动] ✅ 提交 #{_auto_submit_attempts}...", "INFO")
                    print(f"[AUTO] 提交 #{_auto_submit_attempts}", flush=True)
                    if net_collector:
                        net_collector.mark_event("auto_submit", f"attempt={_auto_submit_attempts}")
                    click_submit_button(page)
                    _auto_phase = "wait_result"
                    logger.log(step, "[自动] 等待支付结果...", "INFO")

            elif _auto_phase == "wait_result":
                # 阶段5：等待结果（10秒后检查）
                if since_last_auto >= 10:
                    form_status = _check_form_status(page)
                    has_error = form_status.get("hasError", False)
                    error_msg = form_status.get("errorMessage", "")

                    if has_error or (since_last_auto >= 30):
                        if has_error:
                            logger.log(step, f"[自动] 💳 卡被拒: {error_msg}", "WARN")
                            print(f"[AUTO] 卡被拒: {error_msg[:30]}", flush=True)
                        else:
                            logger.log(step, "[自动] ⏳ 30秒无响应，换卡...", "WARN")

                        if _auto_card_changes < _max_retries:
                            _auto_card_changes += 1
                            logger.log(step, f"[自动] 🔄 换卡 #{_auto_card_changes}/{_max_retries}（保留地址）", "INFO")
                            print(f"[AUTO] 换卡 #{_auto_card_changes}/{_max_retries}", flush=True)
                            if net_collector:
                                net_collector.mark_event("auto_card_change", f"#{_auto_card_changes} err={error_msg[:50]}")

                            requested_bin = card_info.get('bin_prefix', '625003')
                            card_info = rebuild_card_info_for_new_card(card_info, requested_bin)
                            logger.log(step, f"[自动]   新卡号: {card_info['card_number_formatted']}", "SUCCESS")
                            update_card_display(page, card_info)

                            _auto_sleep("换卡后等待", _acfg["retry_wait"], step)
                            _last_auto_action_time = time.time()

                            logger.log(step, "[自动]   仅填充新卡号...", "INFO")
                            fill_plus_form(page, card_info, email, fill_mode="card_only", auto_submit=False)
                            time.sleep(2)
                            _auto_phase = "submit"
                        else:
                            logger.log(step, f"[自动] ❌ 连续 {_max_retries} 次卡被拒，退出", "ERROR")
                            print(f"[AUTO] 失败: 连续{_max_retries}次被拒", flush=True)
                            _auto_phase = "done"
                    else:
                        _last_auto_action_time = time.time()

            elif _auto_phase == "done":
                # 自动驾驶结束 — 立即返回失败
                logger.log(step, f"[自动] 支付失败：连续 {_auto_card_changes} 次卡被拒", "ERROR")
                if net_collector:
                    net_collector.mark_event("auto_done", f"card_changes={_auto_card_changes}")
                    try:
                        _captured = collect_stripe_captured_requests(page)
                        if _captured:
                            net_collector.mark_event("stripe_captured", json.dumps(_captured, ensure_ascii=False, default=str))
                    except Exception:
                        pass
                    trace_path = net_collector.stop_and_save(success=False, result_detail=f"连续{_auto_card_changes}次卡被拒")
                    if trace_path:
                        logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
                return {
                    "success": False,
                    "message": f"连续 {_auto_card_changes} 次卡被拒绝，自动退出",
                    "card_info": card_info,
                    "auto_card_changes": _auto_card_changes,
                    "auto_submit_attempts": _auto_submit_attempts,
                }

            time.sleep(0.5)

        except Exception as e:
            error_msg = str(e)
            if '连接已断开' in error_msg or 'disconnected' in error_msg.lower():
                logger.log(step, f"❌ 浏览器连接已断开", "ERROR")
                if net_collector:
                    trace_path = net_collector.stop_and_save(success=False, result_detail="浏览器连接断开")
                    if trace_path:
                        logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
                return {"success": False, "message": "浏览器连接断开", "card_info": card_info}
            logger.log(step, f"循环异常: {e}", "WARN")
            time.sleep(1)

    logger.log(step, "⏰ 超时", "WARN")
    if net_collector:
        trace_path = net_collector.stop_and_save(success=False, result_detail="超时")
        if trace_path:
            logger.log(step, f"网络 trace 已保存: {trace_path}", "INFO")
    return {"success": False, "message": "超时", "card_info": card_info}


def build_payment_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='ChatGPT 支付脚本 - 优化版（API 直接调用）')
    parser.add_argument('--input', type=str, help='JSON 文件（包含 cookies）')
    parser.add_argument('--checkout-url', type=str, default='', help='已生成的支付链接，存在时优先直接跳转')
    parser.add_argument('--plan', type=str, default='business', choices=['plus', 'business'], help='套餐类型')
    parser.add_argument('--email', type=str, default='', help='邮箱')
    parser.add_argument('--workspace-name', type=str, default='', help='团队空间名称')
    parser.add_argument('--seats', type=int, default=5, help='座位数量')
    parser.add_argument('--country', type=str, default='SG', help='账单国家代码')
    parser.add_argument('--checkout-country', type=str, default='AUTO', help='结账国家代码，AUTO=跟随账单国家')
    parser.add_argument('--currency', type=str, default='USD', help='货币代码')
    parser.add_argument('--max-retries', type=int, default=5, help='最大重试次数')
    parser.add_argument('--timeout', type=int, default=900, help='超时时间秒（默认15分钟）')
    parser.add_argument('--use-proxy', action='store_true', default=False, help='使用显式代理')
    parser.add_argument('--proxy-port', type=int, default=7890, help='代理端口')
    parser.add_argument('--proxy-url', type=str, default='', help='完整代理地址，优先于 --proxy-port')
    parser.add_argument('--paypal-profile-key', type=str, default='', help='复用 PayPal 登录态的持久 Profile Key')
    parser.add_argument(
        '--paypal-profile-bypass-proxy',
        action='store_true',
        default=False,
        help='复用 PayPal 登录态时直连打开浏览器，不附带代理参数',
    )
    parser.add_argument('--headless', action='store_true', default=False, help='无头模式')
    parser.add_argument('--thread-id', type=str, default=None, help='线程标识')
    parser.add_argument('--output', type=str, default=None, help='输出文件')
    parser.add_argument('--quiet', action='store_true', default=False, help='静默模式')
    parser.add_argument('--trigger', type=str, default=None,
                        help='一键触发: fill/submit/new_card/replace_card_only/replace_billing_only/check_ip')
    parser.add_argument('--card-bin', type=str, default='', help='换卡或仅换卡时指定卡头')
    parser.add_argument('--clear-signal', action='store_true', default=False, help='清除信号')
    parser.add_argument(
        '--kr-success-mode',
        type=normalize_kr_success_profile_mode,
        choices=['disabled', 'bin_only', 'split_priority', 'paired_reuse'],
        default=None,
        help='KR 成功资料模式，仅在国家为 KR 时生效',
    )
    parser.add_argument(
        '--yescaptcha-key',
        type=str,
        default='',
        help='YesCaptcha API Key（自动解决 Stripe hCaptcha 3DS 验证）',
    )
    parser.add_argument(
        '--hcaptcha-mode',
        type=str,
        default='abort',
        choices=['abort', 'solve', 'manual'],
        help='hCaptcha 处理模式: abort=立即失败(默认) | solve=调 YesCaptcha 解题 | manual=手动通过且不退出',
    )
    parser.add_argument(
        '--auto-payment-config',
        type=str,
        default='',
        help='自动支付参数 JSON',
    )
    parser.add_argument(
        '--no-auto-fill',
        action='store_true',
        default=False,
        help='禁用自动填充和重试（手动模式）',
    )
    return parser


# ==================== 命令行入口 ====================
if __name__ == "__main__":
    parser = build_payment_arg_parser()
    args = parser.parse_args()

    # 处理触发命令
    if args.trigger:
        trigger_all_browsers(args.trigger, args.card_bin)
        sys.exit(0)

    # 清除信号
    if args.clear_signal:
        clear_signal()
        print(json.dumps({"success": True, "message": "信号已清除"}))
        sys.exit(0)

    if args.quiet:
        logger.quiet = True

    # 确保有输入文件
    if not args.input:
        logger.log("Input", "请指定 --input 参数", "ERROR")
        print(json.dumps({"success": False, "message": "未指定输入文件"}))
        sys.exit(1)

    # 读取输入文件
    logger.log("Input", f"读取文件: {args.input}", "INFO")
    try:
        with open(args.input, 'r', encoding='utf-8') as f:
            data = json.load(f)

        cookies = _extract_payment_cookies_from_payload(data)
        email = args.email or data.get('email', '')
        checkout_url = args.checkout_url or data.get('checkout_url', '')

        if not cookies:
            logger.log("Input", "未找到 Cookies", "ERROR")
            print(json.dumps({"success": False, "message": "未找到 cookies"}))
            sys.exit(1)

        logger.log("Input", f"邮箱: {email}", "INFO")
        logger.log("Input", f"Cookies: {len(cookies)} 个", "INFO")
        if checkout_url:
            logger.log("Input", f"checkout_url: {checkout_url}", "INFO")

    except FileNotFoundError:
        logger.log("Input", f"文件不存在: {args.input}", "ERROR")
        print(json.dumps({"success": False, "message": f"文件不存在: {args.input}"}))
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.log("Input", f"JSON 解析失败: {e}", "ERROR")
        print(json.dumps({"success": False, "message": f"JSON 解析失败: {e}"}))
        sys.exit(1)

    yescaptcha_key = args.yescaptcha_key or os.environ.get("YESCAPTCHA_API_KEY", "")
    if yescaptcha_key:
        os.environ["YESCAPTCHA_API_KEY"] = yescaptcha_key
        logger.log("Input", "YesCaptcha API Key 已配置", "SUCCESS")

    hcaptcha_mode = (args.hcaptcha_mode or os.environ.get("HCAPTCHA_MODE", "abort")).strip().lower()
    os.environ["HCAPTCHA_MODE"] = hcaptcha_mode
    logger.log("Input", f"hCaptcha 模式: {hcaptcha_mode}", "INFO")

    # 设置自动支付配置
    auto_cfg = args.auto_payment_config or os.environ.get("AUTO_PAYMENT_CONFIG", "")
    if auto_cfg:
        os.environ["AUTO_PAYMENT_CONFIG"] = auto_cfg
        logger.log("Input", f"自动支付配置: {auto_cfg[:100]}", "INFO")

    # 自动填充开关
    if args.no_auto_fill:
        os.environ["NO_AUTO_FILL"] = "1"
        logger.log("Input", "自动填充: 关闭（手动模式）", "INFO")
    else:
        logger.log("Input", "自动填充: 开启", "INFO")

    # 执行支付
    result = do_payment(
        cookies=cookies,
        email=email,
        plan_type=args.plan,
        workspace_name=args.workspace_name,
        seat_quantity=args.seats,
        country=args.country,
        checkout_country=args.checkout_country,
        currency=args.currency,
        max_card_retries=args.max_retries,
        timeout=args.timeout,
        use_proxy=args.use_proxy,
        proxy_port=args.proxy_port,
        headless=args.headless,
        thread_id=args.thread_id,
        checkout_url=checkout_url,
        kr_success_mode=args.kr_success_mode,
        proxy_url=args.proxy_url,
        paypal_profile_key=args.paypal_profile_key,
        paypal_profile_bypass_proxy=args.paypal_profile_bypass_proxy,
    )

    # 输出结果
    result_json = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(result_json)
        logger.log("Output", f"结果已保存到: {args.output}", "SUCCESS")

    print(result_json)
    sys.exit(0 if result.get('success') else 1)
