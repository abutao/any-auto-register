"""ChatGPT / Codex CLI 平台插件"""

import json
import random
import string

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from platforms.chatgpt.payment import check_subscription_status

            class _A:
                pass

            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.cookies = extra.get("cookies", "")
            status = check_subscription_status(a, proxy=self.config.proxy if self.config else None)
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    def register(self, email: str = None, password: str = None) -> Account:
        if not password:
            password = "".join(random.choices(string.ascii_letters + string.digits + "!@#$", k=16))

        proxy = self.config.proxy if self.config else None
        extra_config = (self.config.extra or {}) if self.config and getattr(self.config, "extra", None) else {}
        log_fn = getattr(self, "_log_fn", print)

        mail_provider = extra_config.get("mail_provider", "")

        # ── DrissionPage 浏览器注册（CF Worker / Outlook 都走浏览器模式） ──
        if mail_provider in ("cfworker", "outlook"):
            return self._register_drission(email, password, proxy, extra_config, log_fn)

        # ── 其他邮箱 → 纯协议 v2 引擎 ──
        from platforms.chatgpt.protocol_register import ChatGPTProtocolRegister

        reg = ChatGPTProtocolRegister(
            proxy=proxy,
            log_fn=log_fn,
            cookie_dir=extra_config.get("cookie_json_dir", "cookies"),
        )

        if self.mailbox:
            _mailbox = self.mailbox
            _fixed_email = email

            def _resolve_email(candidate_email: str = "") -> str:
                resolved_email = str(_fixed_email or candidate_email or "").strip()
                if not resolved_email:
                    raise RuntimeError("邮箱地址为空")
                return resolved_email

            mail_acct = _mailbox.get_email()
            current_email = _resolve_email(getattr(mail_acct, "email", ""))
            get_current_ids = getattr(_mailbox, "get_current_ids", None)
            before_ids = set(get_current_ids(mail_acct) or []) if callable(get_current_ids) else set()

            otp_timeout = self.get_mailbox_otp_timeout()

            def otp_cb():
                log_fn("等待验证码...")
                code = _mailbox.wait_for_code(
                    mail_acct, keyword="", timeout=otp_timeout, before_ids=before_ids,
                )
                if code:
                    log_fn(f"验证码: {code}")
                return code
        else:
            current_email = email or ""
            otp_cb = None

        if not current_email:
            raise RuntimeError("未获取到邮箱地址")

        result = reg.register(
            email=current_email,
            password=password,
            otp_callback=otp_cb,
        )

        cookies_data = result.get("cookies", {})
        return Account(
            platform="chatgpt",
            email=result["email"],
            password=result["password"],
            status=AccountStatus.REGISTERED,
            extra={
                "cookies": json.dumps(cookies_data) if isinstance(cookies_data, dict) else str(cookies_data),
                "cookie_file": result.get("cookie_file", ""),
                "session_token": result.get("session_token", ""),
                "name": result.get("name", ""),
                "register_mode": "protocol_v2",
            },
        )

    def _register_drission(self, email, password, proxy, extra_config, log_fn) -> Account:
        """使用 DrissionPage 浏览器注册（CF Worker / Outlook）"""
        from platforms.chatgpt.drission_register import register_chatgpt

        mail_provider = extra_config.get("mail_provider", "cfworker")
        headless = str(extra_config.get("drission_headless", "1")).strip().lower() in ("1", "true", "yes", "on")

        # 公共参数
        kwargs = {
            "password": password or "",
            "proxy": proxy or "http://127.0.0.1:7890",
            "headless": headless,
        }

        if mail_provider == "outlook":
            # Outlook: 从邮箱池取账号，邮箱 + Graph API 取码
            if not self.mailbox:
                raise RuntimeError("Outlook 邮箱池未配置")

            # 取一个未注册过 ChatGPT 的 Outlook 账号
            mail_acct = None
            max_skip = 20
            for _ in range(max_skip):
                try:
                    candidate = self.mailbox.get_email()
                except RuntimeError:
                    break
                # 检查是否已注册过 ChatGPT
                candidate_id = int(candidate.account_id or 0)
                if candidate_id:
                    from sqlmodel import Session as _Session
                    from core.db import engine as _engine, OutlookAccountModel
                    with _Session(_engine) as _s:
                        row = _s.get(OutlookAccountModel, candidate_id)
                        if row and row.gpt_register_status == "已注册":
                            # 已注册的不归还，保持 disabled，避免重复取出
                            log_fn(f"[DrissionPage] 跳过已注册账号: {candidate.email}")
                            continue
                mail_acct = candidate
                break

            if not mail_acct:
                raise RuntimeError("Outlook 邮箱池中没有可用的未注册账号")

            outlook_email = mail_acct.email
            outlook_extra = mail_acct.extra or {}

            # 尝试注册，如果邮箱已注册自动换下一个
            max_email_tries = 10
            result = None
            for email_try in range(max_email_tries):
                outlook_email = mail_acct.email
                outlook_extra = mail_acct.extra or {}

                log_fn(f"[DrissionPage] 使用 Outlook 邮箱注册: {outlook_email} (headless={headless})")

                try_kwargs = dict(kwargs)
                try_kwargs["email"] = outlook_email
                try_kwargs["mail_provider"] = "outlook"
                try_kwargs["outlook_client_id"] = outlook_extra.get("client_id", "")
                try_kwargs["outlook_refresh_token"] = outlook_extra.get("refresh_token", "")

                result = register_chatgpt(**try_kwargs)

                if result.get("success"):
                    break

                error_msg = result.get("error", "未知错误")
                account_id = int(mail_acct.account_id or 0)

                if "已注册" in error_msg or "登录页" in error_msg:
                    # 邮箱已注册过，标记并取下一个
                    if account_id:
                        try:
                            from sqlmodel import Session as _S2
                            from core.db import engine as _e2, OutlookAccountModel
                            with _S2(_e2) as _s2:
                                row = _s2.get(OutlookAccountModel, account_id)
                                if row:
                                    row.gpt_register_status = "已注册"
                                    row.enabled = False
                                    _s2.add(row)
                                    _s2.commit()
                        except Exception:
                            pass
                    log_fn(f"[DrissionPage] 邮箱已注册，自动换下一个 ({email_try+1}/{max_email_tries})")

                    # 取下一个
                    mail_acct = None
                    try:
                        mail_acct = self.mailbox.get_email()
                    except RuntimeError:
                        break
                    if not mail_acct:
                        break
                    continue
                else:
                    # 其他失败原因，归还邮箱并停止
                    try:
                        from core.base_mailbox import OutlookMailbox
                        if account_id:
                            OutlookMailbox.return_account(account_id)
                            log_fn(f"[DrissionPage] 注册失败，已归还: {outlook_email}")
                    except Exception:
                        pass
                    raise RuntimeError(f"DrissionPage 注册失败: {error_msg}")

            if not result or not result.get("success"):
                raise RuntimeError("所有 Outlook 邮箱均已注册或不可用")

            register_mode = "drission_outlook"

        else:
            # CF Worker: 自动生成域名邮箱
            cfworker_api_url = extra_config.get("cfworker_api_url", "")
            cfworker_admin_token = extra_config.get("cfworker_admin_token", "")
            cfworker_custom_auth = extra_config.get("cfworker_custom_auth", "")

            cfworker_domain = extra_config.get("cfworker_domain", "")
            enabled_domains = extra_config.get("cfworker_enabled_domains", "")
            if enabled_domains:
                try:
                    domains = json.loads(enabled_domains) if isinstance(enabled_domains, str) else enabled_domains
                    if isinstance(domains, list) and domains:
                        cfworker_domain = random.choice(domains)
                except Exception:
                    pass

            if not cfworker_api_url:
                raise RuntimeError("CF Worker API URL 未配置")

            log_fn(f"[DrissionPage] 使用 CF Worker 域名邮箱注册 (domain={cfworker_domain}, headless={headless})")

            kwargs["email"] = email or ""
            kwargs["cfworker_api_url"] = cfworker_api_url
            kwargs["cfworker_admin_token"] = cfworker_admin_token
            kwargs["cfworker_custom_auth"] = cfworker_custom_auth
            kwargs["cfworker_domain"] = cfworker_domain

            result = register_chatgpt(**kwargs)

            if not result.get("success"):
                raise RuntimeError(f"DrissionPage 注册失败: {result.get('error', '未知错误')}")

            register_mode = "drission_cfworker"

        log_fn(f"[DrissionPage] 注册成功: {result.get('email')}")

        cookies_data = result.get("cookies", {})
        return Account(
            platform="chatgpt",
            email=result["email"],
            password=result.get("password", password),
            status=AccountStatus.REGISTERED,
            extra={
                "cookies": json.dumps(cookies_data) if isinstance(cookies_data, dict) else str(cookies_data),
                "cookie_file": result.get("cookie_file", ""),
                "session_token": result.get("session_token", ""),
                "access_token": result.get("access_token", ""),
                "name": result.get("name", ""),
                "register_mode": register_mode,
                "mail_provider": mail_provider,
            },
        )

    def _action_refresh_oauth(self, account: Account, extra: dict, proxy: str) -> dict:
        """从 Cookie 文件刷新 OAuth，生成 OAuth JSON"""
        import os
        from platforms.chatgpt.cookie_to_oauth import process_cookie_file

        cookie_file = extra.get("cookie_file", "")
        session_token = extra.get("session_token", "")
        cookies_json = extra.get("cookies", "")

        # 优先用 cookie_file，否则从 extra 中的 session_token/cookies 临时构造
        temp_cookie_path = ""
        if cookie_file and os.path.isfile(cookie_file):
            use_cookie_path = cookie_file
        elif session_token or cookies_json:
            # 临时写一个 cookie 文件
            import tempfile
            cookie_data = {"email": account.email, "cookies": {}}
            if cookies_json:
                try:
                    cookie_data["cookies"] = json.loads(cookies_json) if isinstance(cookies_json, str) else cookies_json
                except Exception:
                    pass
            if session_token and "__Secure-next-auth.session-token" not in cookie_data["cookies"]:
                cookie_data["cookies"]["__Secure-next-auth.session-token"] = session_token
            fd, temp_cookie_path = tempfile.mkstemp(suffix=".json", prefix="oauth_tmp_")
            with os.fdopen(fd, "w") as f:
                json.dump(cookie_data, f)
            use_cookie_path = temp_cookie_path
        else:
            return {"ok": False, "error": "缺少 Cookie 文件或 Session Token，无法刷新 OAuth"}

        try:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "oauth_out")
            result = process_cookie_file(
                cookie_path=use_cookie_path,
                output_dir=output_dir,
                proxy=proxy,
                email_hint=account.email,
            )

            if not result.get("success"):
                return {"ok": False, "error": result.get("error", "OAuth 刷新失败")}

            return {
                "ok": True,
                "data": {
                    "message": f"OAuth 刷新成功，文件: {result['file_path']}",
                    "file_path": result["file_path"],
                    "account_id": result.get("account_id", ""),
                    "expired": result.get("expired", ""),
                    "plan_type": result.get("resolved_plan_type", ""),
                },
                "account_extra_patch": {
                    "oauth_file": result["file_path"],
                    "chatgpt_account_id": result.get("account_id", ""),
                    "chatgpt_plan_type": result.get("resolved_plan_type", ""),
                },
            }
        except Exception as e:
            return {"ok": False, "error": f"OAuth 刷新异常: {e}"}
        finally:
            if temp_cookie_path and os.path.isfile(temp_cookie_path):
                try:
                    os.unlink(temp_cookie_path)
                except Exception:
                    pass

    def get_platform_actions(self) -> list:
        return [
            {"id": "refresh_oauth", "label": "刷新 OAuth", "params": []},
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {
                "id": "payment_link",
                "label": "生成支付链接",
                "params": [
                    {"key": "country", "label": "地区", "type": "select", "options": ["US", "SG", "TR", "HK", "JP", "GB", "AU", "CA"]},
                    {"key": "plan", "label": "套餐", "type": "select", "options": ["plus", "team"]},
                ],
            },
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_sub2api",
                "label": "上传 Sub2API",
                "params": [
                    {"key": "api_url", "label": "Sub2API API URL", "type": "text"},
                    {"key": "api_key", "label": "Sub2API API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_tm",
                "label": "上传 Team Manager",
                "params": [
                    {"key": "api_url", "label": "TM API URL", "type": "text"},
                    {"key": "api_key", "label": "TM API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_codex_proxy",
                "label": "上传 CodexProxy",
                "params": [
                    {"key": "api_url", "label": "API URL", "type": "text"},
                    {"key": "api_key", "label": "Admin Key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        proxy = self.config.proxy if self.config else None
        if not proxy:
            from core.config_store import config_store
            proxy = config_store.get("default_proxy", "") or "http://127.0.0.1:7890"
        extra = account.extra or {}

        class _A:
            pass

        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id

        if action_id == "refresh_oauth":
            return self._action_refresh_oauth(account, extra, proxy)

        if action_id == "probe_local_status":
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status

            probe_result = probe_local_chatgpt_status(a, proxy=proxy)
            summary = (
                f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
            )
            return {
                "ok": True,
                "data": {
                    "message": f"本地状态探测完成：{summary}",
                    "probe": probe_result,
                },
                "account_extra_patch": {
                    "chatgpt_local": probe_result,
                },
            }

        if action_id == "sync_cliproxyapi_status":
            from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status

            sync_result = sync_chatgpt_cliproxyapi_status(a)
            ok = bool(sync_result.get("uploaded")) and sync_result.get("remote_state") not in {"unreachable", "not_found"}
            summary = (
                f"远端状态={sync_result.get('status') or 'not_found'}, "
                f"探测={sync_result.get('remote_state') or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"CLIProxyAPI 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "cliproxyapi": sync_result,
                    },
                },
            }

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)
            if result.success:
                return {
                    "ok": True,
                    "data": {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "payment_link":
            from platforms.chatgpt.payment import generate_plus_link, generate_team_link

            plan = params.get("plan", "plus")
            country = params.get("country", "US")
            if plan == "plus":
                url = generate_plus_link(a, proxy=proxy, country=country)
            else:
                url = generate_team_link(
                    a,
                    workspace_name=params.get("workspace_name", "MyTeam"),
                    price_interval=params.get("price_interval", "month"),
                    seat_quantity=int(params.get("seat_quantity", 5) or 5),
                    proxy=proxy,
                    country=country,
                )
            return {"ok": bool(url), "data": {"url": url}}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_sub2api":
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager

            ok, msg = upload_to_team_manager(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_codex_proxy":
            upload_type = str(
                params.get("upload_type")
                or (self.config.extra or {}).get("codex_proxy_upload_type")
                or "at"
            ).strip().lower()

            if upload_type == "rt":
                from platforms.chatgpt.cpa_upload import upload_to_codex_proxy

                ok, msg = upload_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            else:
                from platforms.chatgpt.cpa_upload import upload_at_to_codex_proxy

                ok, msg = upload_at_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"未知操作: {action_id}")
