#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hcaptcha_solver.py
==================
Stripe 支付流程中 hCaptcha 3DS 验证的自动解决方案。
支持 YesCaptcha API 自动过验证。

Stripe 在高风险交易时会弹出 hCaptcha 验证（通过 hcaptcha-inner iframe），
本模块检测该 iframe，提取参数，调用 YesCaptcha 解题，然后注入 token。

用法（集成到支付脚本中）:
    from hcaptcha_solver import detect_and_solve_hcaptcha
    result = detect_and_solve_hcaptcha(page, api_key="YOUR_YESCAPTCHA_KEY")
"""

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlencode

import requests


# YesCaptcha API 配置
YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"
YESCAPTCHA_CREATE_TASK = f"{YESCAPTCHA_API_BASE}/createTask"
YESCAPTCHA_GET_RESULT = f"{YESCAPTCHA_API_BASE}/getTaskResult"

# 默认超时
SOLVE_TIMEOUT = 120  # 最多等 120 秒
POLL_INTERVAL = 5    # 每 5 秒查一次

# hCaptcha iframe 检测 JS
_DETECT_HCAPTCHA_JS = '''
(function() {
    // 查找 Stripe 的 hCaptcha iframe
    const iframes = document.querySelectorAll('iframe');
    for (const iframe of iframes) {
        const src = iframe.src || '';
        if (src.includes('hcaptcha-inner') || src.includes('hcaptcha-invisible') || src.includes('hcaptcha')) {
            // 提取 hash 参数
            const hash = src.split('#')[1] || '';
            const params = {};
            for (const kv of hash.split('&')) {
                const idx = kv.indexOf('=');
                const k = idx >= 0 ? kv.slice(0, idx) : kv;
                const v = idx >= 0 ? kv.slice(idx + 1) : '';
                if (k) params[decodeURIComponent(k)] = decodeURIComponent(v || '');
            }
            return {
                found: true,
                src: src.substring(0, 500),
                sitekey: params.sitekey || '',
                rqdata: params.rqdata || '',
                intentId: params.intentId || '',
                clientSecret: params.clientSecret || '',
                verifyUrl: params.verifyUrl || '',
                referrer: params.referrer || '',
                controllerId: params.controllerId || '',
                visible: iframe.offsetWidth > 0 && iframe.offsetHeight > 0,
            };
        }
    }
    return { found: false };
})();
'''

# 注入 hCaptcha token 的 JS（通过 Stripe 的内部回调机制）
_INJECT_TOKEN_JS_TEMPLATE = '''
(function() {{
    // 方式1: 直接调用 Stripe 的 hCaptcha 回调
    try {{
        // Stripe 在 window 上注册了一个 controller
        const controllerId = '{controller_id}';
        if (window[controllerId] && window[controllerId].hcaptchaCallback) {{
            window[controllerId].hcaptchaCallback('{token}');
            return {{ success: true, method: 'controller_callback' }};
        }}
    }} catch(e) {{}}

    // 方式2: 找到 hCaptcha iframe 并通过 postMessage 传递 token
    try {{
        const iframes = document.querySelectorAll('iframe');
        for (const iframe of iframes) {{
            if ((iframe.src || '').includes('hcaptcha')) {{
                iframe.contentWindow.postMessage({{
                    source: 'hcaptcha',
                    label: 'challenge-closed',
                    response: '{token}',
                    event: 'challenge-passed',
                }}, '*');
                return {{ success: true, method: 'postMessage' }};
            }}
        }}
    }} catch(e) {{}}

    // 方式3: 设置全局 hCaptcha response 并触发回调
    try {{
        if (window.hcaptcha) {{
            // 直接设置 response
            const containers = document.querySelectorAll('[data-hcaptcha-widget-id]');
            for (const c of containers) {{
                const widgetId = c.getAttribute('data-hcaptcha-widget-id');
                if (widgetId) {{
                    window.hcaptcha.execute(widgetId, {{ async: false }});
                }}
            }}
        }}
    }} catch(e) {{}}

    return {{ success: false, error: 'no callback found' }};
}})();
'''


def _log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": "ℹ️", "SUCCESS": "✓", "ERROR": "✗", "WARN": "⚠"}.get(level, "")
    print(f"[{ts}] [hCaptcha] {prefix} {msg}", file=sys.stderr)


def detect_hcaptcha(page) -> Optional[Dict[str, Any]]:
    """检测页面中是否有 Stripe 的 hCaptcha 验证 iframe。"""
    try:
        result = page.run_js(_DETECT_HCAPTCHA_JS)
        if result and result.get("found") and result.get("src"):
            parsed = _parse_hcaptcha_url(result["src"])
            result = _merge_hcaptcha_info(result, parsed)
        if result and result.get("found") and result.get("sitekey"):
            return result
    except Exception:
        pass

    # 备选：直接搜索 iframe 元素
    try:
        iframes = page.eles('tag:iframe')
        for iframe in iframes:
            src = iframe.attr('src') or ''
            if 'hcaptcha' in src.lower():
                # 从 src 提取参数
                params = _parse_hcaptcha_url(src)
                if params.get("sitekey"):
                    return params
    except Exception:
        pass

    return None


def _parse_hcaptcha_url(url: str) -> Dict[str, Any]:
    """从 hCaptcha iframe URL 中提取参数。"""
    result = {"found": True, "src": url[:500]}
    if "#" in url:
        hash_part = url.split("#", 1)[1]
        for kv in hash_part.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                k = unquote(k)
                v = unquote(v)
                if k in ("sitekey", "rqdata", "intentId", "clientSecret", "verifyUrl", "referrer", "controllerId"):
                    result[k] = v
    return result


def _merge_hcaptcha_info(primary: Optional[Dict[str, Any]], secondary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(secondary or {})
    for key, value in (primary or {}).items():
        if value not in ("", None):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def _normalize_verify_url(verify_url: str) -> str:
    if verify_url.startswith("/"):
        return f"https://api.stripe.com{verify_url}"
    return verify_url


def _build_verify_payload(client_secret: str, token: str) -> Dict[str, str]:
    return {
        "client_secret": client_secret,
        "hcaptcha_token": token,
    }


def _build_controller_injection_js(token: str, controller_id: str = "") -> str:
    token_js = json.dumps(token)
    controller_js = json.dumps(controller_id or "")
    return f'''
    (function() {{
        const token = {token_js};
        const controllerId = {controller_js};
        const attempts = [];
        const seen = new Set();
        const controllers = [];

        const record = (success, method, error) => {{
            attempts.push({{
                success: !!success,
                method: method || '',
                error: error ? String(error).slice(0, 200) : '',
            }});
        }};

        const addController = (candidate, name) => {{
            if (!candidate) return;
            const t = typeof candidate;
            if (t !== 'object' && t !== 'function') return;
            if (seen.has(candidate)) return;
            seen.add(candidate);
            controllers.push({{ candidate, name: name || '' }});
        }};

        try {{
            if (controllerId && window[controllerId]) {{
                addController(window[controllerId], controllerId);
            }}
        }} catch (e) {{
            record(false, controllerId || 'controller_lookup', e);
        }}

        try {{
            for (const key of Object.keys(window)) {{
                if (key === controllerId || key.startsWith('__privateStripe')) {{
                    addController(window[key], key);
                }}
            }}
        }} catch (e) {{
            record(false, 'window_scan', e);
        }}

        const directNames = [
            'hcaptchaCallback',
            'onHcaptchaToken',
            'onHcaptchaResponse',
            'handleHcaptchaToken',
            'handleHcaptchaResponse',
            'completeHcaptchaChallenge',
            'verifyChallengeResponse',
        ];

        for (const item of controllers) {{
            const ctrl = item.candidate;
            const base = item.name || 'controller';

            for (const methodName of directNames) {{
                try {{
                    if (typeof ctrl[methodName] === 'function') {{
                        ctrl[methodName](token);
                        return {{ success: true, method: `${{base}}.${{methodName}}`, attempts }};
                    }}
                }} catch (e) {{
                    record(false, `${{base}}.${{methodName}}`, e);
                }}
            }}

            try {{
                if (ctrl._hcaptchaFrameRef && typeof ctrl._hcaptchaFrameRef.onMessage === 'function') {{
                    const msg = {{
                        source: 'stripe-hcaptcha-iframe',
                        type: 'hcaptcha-challenge-response',
                        payload: {{
                            response: token,
                            event: 'challenge-passed',
                        }},
                    }};
                    ctrl._hcaptchaFrameRef.onMessage(msg);
                    return {{ success: true, method: `${{base}}._hcaptchaFrameRef.onMessage`, attempts }};
                }}
            }} catch (e) {{
                record(false, `${{base}}._hcaptchaFrameRef.onMessage`, e);
            }}

            try {{
                const proto = Object.getPrototypeOf(ctrl) || {{}};
                for (const prop of Object.getOwnPropertyNames(proto)) {{
                    try {{
                        if (typeof ctrl[prop] === 'function' && /captcha|challenge/i.test(prop)) {{
                            ctrl[prop](token);
                            return {{ success: true, method: `${{base}}.${{prop}}`, attempts }};
                        }}
                    }} catch (e) {{
                        record(false, `${{base}}.${{prop}}`, e);
                    }}
                }}
            }} catch (e) {{
                record(false, `${{base}}.__proto__`, e);
            }}
        }}

        const msg = {{
            source: 'stripe-hcaptcha-iframe',
            type: 'hcaptcha-challenge-response',
            payload: {{
                response: token,
                event: 'challenge-passed',
            }},
        }};

        try {{
            window.postMessage(msg, '*');
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {{
                if ((iframe.src || '').includes('hcaptcha') && iframe.contentWindow) {{
                    try {{
                        iframe.contentWindow.postMessage(msg, '*');
                    }} catch (e) {{
                        record(false, 'iframe.postMessage', e);
                    }}
                }}
            }}
            return {{ success: true, method: 'window.postMessage', attempts }};
        }} catch (e) {{
            record(false, 'window.postMessage', e);
        }}

        return {{ success: false, error: 'no callback found', attempts }};
    }})();
    '''


def _build_verify_fetch_js(full_url: str, payload: Dict[str, str]) -> str:
    url_js = json.dumps(full_url)
    payload_js = json.dumps(payload, ensure_ascii=False)
    return f'''
    (async function() {{
        try {{
            const payload = {payload_js};
            const params = new URLSearchParams();
            for (const [key, value] of Object.entries(payload)) {{
                params.append(key, value == null ? '' : String(value));
            }}
            const resp = await fetch({url_js}, {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json, text/plain, */*",
                }},
                body: params.toString(),
                credentials: "include",
            }});
            let text = "";
            try {{
                text = await resp.text();
            }} catch (e) {{}}
            return {{
                success: resp.ok,
                status: resp.status,
                body: (text || '').substring(0, 500),
                data: (text || '').substring(0, 500),
            }};
        }} catch(e) {{
            return {{ success: false, error: e.message }};
        }}
    }})();
    '''


def _wait_for_hcaptcha_clear(
    page,
    original_info: Dict[str, Any],
    wait_seconds: float = 8.0,
    poll_interval: float = 1.0,
) -> bool:
    deadline = time.time() + max(wait_seconds, poll_interval)
    original_intent = str(original_info.get("intentId", "") or "").strip()
    last_info: Optional[Dict[str, Any]] = None

    while time.time() <= deadline:
        try:
            current = detect_hcaptcha(page)
        except Exception as exc:
            _log(f"复查 hCaptcha 状态失败: {exc}", "WARN")
            current = None

        last_info = current
        if not current or not current.get("found"):
            return True

        current_intent = str(current.get("intentId", "") or "").strip()
        if original_intent and current_intent and current_intent != original_intent:
            return True

        time.sleep(poll_interval)

    if last_info and last_info.get("found"):
        _log(
            f"hCaptcha 仍存在: intent={last_info.get('intentId', '')} visible={last_info.get('visible')}",
            "WARN",
        )
    return False


def solve_hcaptcha_yescaptcha(
    api_key: str,
    sitekey: str,
    website_url: str,
    rqdata: str = "",
    timeout: int = SOLVE_TIMEOUT,
) -> Optional[str]:
    """
    调用 YesCaptcha API 解决 hCaptcha。

    Returns:
        hCaptcha token 字符串，或 None（失败）
    """
    _log(f"提交 hCaptcha 任务: sitekey={sitekey[:20]}... url={website_url[:60]}", "INFO")

    task = {
        "type": "HCaptchaTaskProxyless",
        "websiteURL": website_url,
        "websiteKey": sitekey,
    }

    # 如果有企业参数 rqdata
    if rqdata:
        task["isEnterprise"] = True
        task["enterprisePayload"] = {"rqdata": rqdata}

    create_payload = {
        "clientKey": api_key,
        "task": task,
    }

    try:
        resp = requests.post(YESCAPTCHA_CREATE_TASK, json=create_payload, timeout=30)
        resp_data = resp.json()
    except Exception as e:
        _log(f"创建任务失败: {e}", "ERROR")
        return None

    if resp_data.get("errorId"):
        _log(f"创建任务错误: {resp_data.get('errorDescription', resp_data)}", "ERROR")
        return None

    task_id = resp_data.get("taskId")
    if not task_id:
        _log(f"未获取到 taskId: {resp_data}", "ERROR")
        return None

    _log(f"任务已创建: taskId={task_id}", "SUCCESS")

    # 轮询等待结果
    start_time = time.time()
    while time.time() - start_time < timeout:
        time.sleep(POLL_INTERVAL)

        try:
            result_resp = requests.post(
                YESCAPTCHA_GET_RESULT,
                json={"clientKey": api_key, "taskId": task_id},
                timeout=30,
            )
            result_data = result_resp.json()
        except Exception as e:
            _log(f"查询结果失败: {e}", "WARN")
            continue

        status = result_data.get("status", "")
        if status == "ready":
            solution = result_data.get("solution", {})
            token = solution.get("gRecaptchaResponse") or solution.get("token") or ""
            if token:
                _log(f"hCaptcha 解决成功! token长度={len(token)}", "SUCCESS")
                return token
            else:
                _log(f"解决成功但无 token: {result_data}", "ERROR")
                return None

        if status == "failed" or result_data.get("errorId"):
            _log(f"解题失败: {result_data.get('errorDescription', result_data)}", "ERROR")
            return None

        elapsed = int(time.time() - start_time)
        _log(f"等待解题中... ({elapsed}s/{timeout}s)", "INFO")

    _log(f"解题超时 ({timeout}s)", "ERROR")
    return None


def inject_hcaptcha_token(page, token: str, hcaptcha_info: Dict) -> bool:
    """将 hCaptcha token 注入到页面中，触发 Stripe 的验证回调。"""
    _log(f"注入 hCaptcha token (长度={len(token)})", "INFO")

    client_secret = hcaptcha_info.get("clientSecret", "")
    verify_url = hcaptcha_info.get("verifyUrl", "")
    controller_id = hcaptcha_info.get("controllerId", "")

    # ---- 方式0（新增，最可靠）: 在 hCaptcha iframe 内部直接设置 response ----
    _log("方式0: hCaptcha iframe 内部注入", "INFO")
    try:
        token_js = json.dumps(token)
        hcaptcha_iframe_js = f'''
        (function() {{
            // 设置 h-captcha-response textarea
            var ta = document.querySelector('textarea[name="h-captcha-response"]');
            if (ta) ta.value = {token_js};
            var ta2 = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta2) ta2.value = {token_js};

            // 尝试调用 hcaptcha 的回调
            if (window.hcaptcha) {{
                try {{
                    // 获取所有 widget
                    var widgetIds = [];
                    document.querySelectorAll('[data-hcaptcha-widget-id]').forEach(function(el) {{
                        widgetIds.push(el.getAttribute('data-hcaptcha-widget-id'));
                    }});
                    if (widgetIds.length === 0) widgetIds = ['0'];
                    for (var i = 0; i < widgetIds.length; i++) {{
                        try {{ window.hcaptcha.execute(widgetIds[i]); }} catch(e) {{}}
                    }}
                }} catch(e) {{}}
            }}

            // 触发所有可能的回调函数
            var callbacks = ['onHcaptchaToken','hcaptchaCallback','onVerify','dataCallback'];
            for (var i = 0; i < callbacks.length; i++) {{
                if (typeof window[callbacks[i]] === 'function') {{
                    try {{ window[callbacks[i]]({token_js}); return {{success:true, method:'iframe.'+callbacks[i]}}; }} catch(e) {{}}
                }}
            }}

            // 通过 parent.postMessage 通知 Stripe
            try {{
                window.parent.postMessage({{
                    source: 'hcaptcha',
                    label: 'challenge-closed',
                    contents: {{
                        event: 'challenge-passed',
                        response: {token_js},
                        expiration: 120
                    }}
                }}, '*');
                return {{success: true, method: 'iframe.parent.postMessage'}};
            }} catch(e) {{}}

            return {{success: ta ? true : false, method: 'textarea_only'}};
        }})();
        '''

        iframes = page.eles('tag:iframe')
        for iframe in iframes:
            src = iframe.attr('src') or ''
            if 'hcaptcha' in src.lower():
                try:
                    pf = page.get_frame(iframe)
                    result = pf.run_js(hcaptcha_iframe_js)
                    _log(f"hCaptcha iframe 注入结果: {result}", "INFO")
                    if result and result.get("success"):
                        time.sleep(3)
                        if _wait_for_hcaptcha_clear(page, hcaptcha_info):
                            _log("✓ hCaptcha iframe 注入成功，challenge 已消失", "SUCCESS")
                            return True
                        _log("hCaptcha iframe 注入已执行，但 challenge 仍存在，继续...", "WARN")
                except Exception as e:
                    _log(f"hCaptcha iframe 注入失败: {e}", "WARN")
    except Exception as e:
        _log(f"方式0 失败: {e}", "WARN")

    # ---- 方式1: 优先直接使用 Stripe controller / postMessage 回调 ----
    _log("方式1: controller / postMessage 回调", "INFO")
    try:
        controller_result = page.run_js(_build_controller_injection_js(token, controller_id))
        _log(f"controller 注入结果: {controller_result}", "INFO")
        if controller_result and controller_result.get("success"):
            if _wait_for_hcaptcha_clear(page, hcaptcha_info):
                _log("✓ controller 回调成功，challenge 已消失", "SUCCESS")
                return True
            _log("controller 回调已执行，但 challenge 仍存在，继续 fallback", "WARN")
    except Exception as e:
        _log(f"方式1 失败: {e}", "WARN")

    # ---- 方式2: 跳过（浏览器端 JS fetch api.stripe.com 会触发 401 认证弹框） ----
    # api.stripe.com 返回 WWW-Authenticate: Basic → 浏览器弹原生用户名密码框
    # 直接跳到方式3（Python requests，不会弹框）
    if verify_url and client_secret:
        _log("方式2: 跳过（会触发浏览器认证弹框）", "INFO")

    # ---- 方式3: 直接 Python requests 调 verify_challenge ----
    if verify_url and client_secret:
        _log("方式3: Python requests verify_challenge", "INFO")
        try:
            full_url = _normalize_verify_url(verify_url)
            api_resp = requests.post(
                full_url,
                data=_build_verify_payload(client_secret, token),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                },
                timeout=30,
            )
            _log(f"verify_challenge 响应: {api_resp.status_code} {api_resp.text[:200]}", "INFO")
            if api_resp.status_code == 200:
                if _wait_for_hcaptcha_clear(page, hcaptcha_info):
                    _log("✓ verify_challenge 成功（Python requests）!", "SUCCESS")
                    return True
                _log("Python verify 返回 200，但 challenge 仍存在，尝试再次同步 controller", "WARN")
                try:
                    controller_result = page.run_js(_build_controller_injection_js(token, controller_id))
                    _log(f"Python verify 后 controller 同步结果: {controller_result}", "INFO")
                    if controller_result and controller_result.get("success") and _wait_for_hcaptcha_clear(page, hcaptcha_info):
                        _log("✓ Python verify 后 controller 同步成功", "SUCCESS")
                        return True
                except Exception as sync_exc:
                    _log(f"Python verify 后 controller 同步失败: {sync_exc}", "WARN")
        except Exception as e:
            _log(f"方式3 失败: {e}", "WARN")

    return False


def detect_and_solve_hcaptcha(
    page,
    api_key: str = "",
    timeout: int = SOLVE_TIMEOUT,
) -> Dict[str, Any]:
    """
    检测并自动解决 Stripe 支付流程中的 hCaptcha 验证。

    Args:
        page: DrissionPage 的 ChromiumPage 实例
        api_key: YesCaptcha API Key
        timeout: 解题超时时间

    Returns:
        {"solved": bool, "message": str, ...}
    """
    if not api_key:
        api_key = os.environ.get("YESCAPTCHA_API_KEY", "")
    if not api_key:
        return {"solved": False, "message": "未配置 YesCaptcha API Key"}

    # 1. 检测 hCaptcha
    info = detect_hcaptcha(page)
    if not info or not info.get("found"):
        return {"solved": False, "message": "未检测到 hCaptcha", "detected": False}

    sitekey = info.get("sitekey", "")
    rqdata = info.get("rqdata", "")
    referrer = info.get("referrer", "")

    if not sitekey:
        return {"solved": False, "message": "未提取到 sitekey"}

    _log(f"检测到 hCaptcha! sitekey={sitekey[:20]}...", "WARN")

    # 用 referrer 作为 websiteURL，如果没有就用当前页面 URL
    website_url = referrer or page.url or "https://chatgpt.com"

    # 2. 调用 YesCaptcha 解题
    token = solve_hcaptcha_yescaptcha(
        api_key=api_key,
        sitekey=sitekey,
        website_url=website_url,
        rqdata=rqdata,
        timeout=timeout,
    )

    if not token:
        return {"solved": False, "message": "YesCaptcha 解题失败", "detected": True}

    # 3. 注入 token
    injected = inject_hcaptcha_token(page, token, info)

    if injected:
        _log("hCaptcha 验证已通过!", "SUCCESS")
        return {"solved": True, "message": "hCaptcha 已解决", "token_length": len(token)}
    else:
        return {
            "solved": False,
            "message": "token 注入失败",
            "token": token[:50] + "...",
            "detected": True,
        }
