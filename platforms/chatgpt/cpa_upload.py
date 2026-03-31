"""
CPA (Codex Protocol API) 上传功能
"""

import json
import base64
import logging
from typing import Tuple, Any, Optional
from datetime import datetime, timezone, timedelta
import hashlib

from curl_cffi import requests as cffi_requests
from curl_cffi import CurlMime

logger = logging.getLogger(__name__)


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _b64url_json(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _derive_display_name(email: str) -> str:
    local = (email or "").split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ")
    parts = [part for part in local.split() if part]
    if not parts:
        return "OpenAI User"
    return " ".join(part[:1].upper() + part[1:] for part in parts[:3])


def _get_auth_info(payload: dict) -> dict:
    nested = payload.get("https://api.openai.com/auth", {})
    if isinstance(nested, dict) and nested:
        return nested

    flat = {}
    for key, value in payload.items():
        if key.startswith("https://api.openai.com/auth."):
            flat[key.split(".", 4)[-1]] = value
    return flat


def _build_compat_id_token(
    *,
    access_token: str,
    email: str,
) -> str:
    """
    基于 access_token 构造一个仅供本地 CPA/玩具环境解析的兼容 id_token。
    注意：该 token 仅用于不校验签名、只解析 payload 的本地兼容场景。
    """
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return ""

    auth_info = _get_auth_info(payload)
    email_from_token = ((payload.get("https://api.openai.com/profile") or {}).get("email") or payload.get("email") or email or "").strip()
    email_verified = bool(
        ((payload.get("https://api.openai.com/profile") or {}).get("email_verified"))
        if isinstance(payload.get("https://api.openai.com/profile"), dict)
        else payload.get("email_verified", True)
    )
    account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    user_id = str(
        auth_info.get("chatgpt_user_id")
        or auth_info.get("user_id")
        or payload.get("sub")
        or ""
    ).strip()
    iat = int(payload.get("iat") or 0)
    exp = int(payload.get("exp") or 0)
    auth_time = int(payload.get("pwd_auth_time") or payload.get("auth_time") or iat or 0)
    session_id = str(payload.get("session_id") or f"compat_session_{(account_id or user_id or 'unknown').replace('-', '')[:24]}").strip()
    plan_type = str(auth_info.get("chatgpt_plan_type") or "free").strip() or "free"
    organization_id = str(auth_info.get("organization_id") or f"org-{hashlib.sha1((account_id or email_from_token or user_id).encode('utf-8')).hexdigest()[:24]}")
    project_id = str(auth_info.get("project_id") or f"proj_{hashlib.sha1((organization_id + ':' + (account_id or user_id)).encode('utf-8')).hexdigest()[:24]}")

    compat_auth = {
        "chatgpt_account_id": account_id,
        "chatgpt_plan_type": plan_type,
        "chatgpt_subscription_active_start": auth_info.get("chatgpt_subscription_active_start"),
        "chatgpt_subscription_active_until": auth_info.get("chatgpt_subscription_active_until"),
        "chatgpt_subscription_last_checked": auth_info.get("chatgpt_subscription_last_checked"),
        "chatgpt_user_id": user_id,
        "completed_platform_onboarding": bool(auth_info.get("completed_platform_onboarding", False)),
        "groups": auth_info.get("groups", []),
        "is_org_owner": bool(auth_info.get("is_org_owner", True)),
        "localhost": bool(auth_info.get("localhost", True)),
        "organization_id": organization_id,
        "organizations": auth_info.get("organizations") or [
            {
                "id": organization_id,
                "is_default": True,
                "role": "owner",
                "title": "Personal",
            }
        ],
        "project_id": project_id,
        "user_id": str(auth_info.get("user_id") or user_id or "").strip(),
    }

    compat_payload = {
        "amr": ["pwd", "otp", "mfa", "urn:openai:amr:otp_email"],
        "at_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:22],
        "aud": ["app_EMoamEEZ73f0CkXaXp7hrann"],
        "auth_provider": "password",
        "auth_time": auth_time,
        "email": email_from_token,
        "email_verified": email_verified,
        "exp": exp,
        "https://api.openai.com/auth": compat_auth,
        "iat": iat,
        "iss": payload.get("iss") or "https://auth.openai.com",
        "jti": f"compat-{hashlib.sha1(access_token.encode('utf-8')).hexdigest()[:32]}",
        "name": _derive_display_name(email_from_token),
        "rat": auth_time,
        "sid": session_id,
        "sub": payload.get("sub") or user_id,
    }

    header = {
        "alg": "RS256",
        "typ": "JWT",
        "kid": "compat",
    }
    signature = _b64url_bytes(b"compat_signature_for_cpa_parsing_only")
    return f"{_b64url_json(header)}.{_b64url_json(compat_payload)}.{signature}"


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store
        return config_store.get(key, "")
    except Exception:
        return ""


def _to_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    text = str(v).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _parse_group_ids(v: Any) -> list[int]:
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            try:
                out.append(int(x))
            except Exception:
                continue
        return out

    text = str(v).strip()
    if not text:
        return []
    try:
        if text.startswith("["):
            arr = json.loads(text)
            if isinstance(arr, list):
                return [int(x) for x in arr if str(x).strip()]
    except Exception:
        pass

    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except Exception:
            continue
    return out


def _build_proxy_key(proxy_url: str) -> str:
    """从代理URL构建 proxy_key: protocol|host|port||"""
    try:
        normalized = str(proxy_url or "").strip()
        if not normalized:
            return ""
        protocol = "http"
        if "://" in normalized:
            protocol, normalized = normalized.split("://", 1)
        auth, _, hostport = normalized.rpartition("@")
        target = hostport if hostport else auth
        if ":" in target:
            host, port_text = target.rsplit(":", 1)
            port = int(port_text)
        else:
            host, port = target, 0
        host = str(host or "").strip()
        if not host:
            return ""
        return f"{protocol}|{host}|{port}||"
    except Exception:
        return ""


def _sub2api_auth_headers_list(api_key: str) -> list[dict]:
    key = str(api_key or "").strip()
    if not key:
        return [{}]
    return [
        {"X-API-Key": key},
        {"Authorization": f"Bearer {key}"},
        {"Authorization": key},
        {"api-key": key},
    ]


def _extract_items(data: Any) -> list[dict]:
    if isinstance(data, dict):
        payload = data.get("data")
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return [x for x in payload.get("items") if isinstance(x, dict)]
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(data.get("items"), list):
            return [x for x in data.get("items") if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def ensure_sub2api_proxy(
    proxy_key: str,
    api_url: str,
    api_key: str,
) -> Tuple[Optional[int], str]:
    """确保 sub2api 存在对应代理，返回 proxy_id。"""
    if not proxy_key:
        return None, "proxy_key 为空"

    parts = str(proxy_key).split("|")
    if len(parts) < 3:
        return None, f"proxy_key 格式错误: {proxy_key}"

    protocol = str(parts[0] or "http").strip() or "http"
    host = str(parts[1] or "").strip()
    try:
        port = int(parts[2] or 0)
    except Exception:
        return None, f"proxy_key 端口非法: {proxy_key}"

    if not host:
        return None, "proxy_key host 为空"

    url = api_url.rstrip("/") + "/api/v1/admin/proxies"
    auth_headers_list = _sub2api_auth_headers_list(api_key)

    for auth_headers in auth_headers_list:
        try:
            resp = cffi_requests.get(
                url,
                headers=auth_headers,
                params={"page": 1, "page_size": 200},
                verify=False,
                timeout=20,
                impersonate="chrome110",
            )
            if resp.status_code == 200:
                items = _extract_items(resp.json())
                for item in items:
                    item_id = item.get("id")
                    item_key = str(item.get("proxy_key") or "").strip()
                    item_host = str(item.get("host") or "").strip()
                    try:
                        item_port = int(item.get("port") or 0)
                    except Exception:
                        item_port = 0
                    item_protocol = str(item.get("protocol") or "").strip().lower()
                    if item_key and item_key == proxy_key and item_id:
                        return int(item_id), "代理已存在"
                    if item_host == host and item_port == port and (not item_protocol or item_protocol == protocol.lower()):
                        if item_id:
                            return int(item_id), "代理已存在"
                break
        except Exception:
            continue

    create_payload = {
        "url": f"{protocol}://{host}:{port}",
    }
    for auth_headers in auth_headers_list:
        try:
            resp = cffi_requests.post(
                url,
                headers={"Content-Type": "application/json", **auth_headers},
                json=create_payload,
                verify=False,
                timeout=20,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                items = _extract_items(data)
                if items and items[0].get("id"):
                    return int(items[0]["id"]), "代理创建成功"
                if isinstance(data, dict):
                    d = data.get("data")
                    if isinstance(d, dict) and d.get("id"):
                        return int(d["id"]), "代理创建成功"
                    if data.get("id"):
                        return int(data["id"]), "代理创建成功"
                # 创建成功但返回格式未知，再次查找一次
                return ensure_sub2api_proxy(proxy_key=proxy_key, api_url=api_url, api_key=api_key)
        except Exception:
            continue

    return None, "代理创建/查询失败"


def search_sub2api_account(
    name: str,
    api_url: str,
    api_key: str,
) -> Tuple[Optional[int], str]:
    """按关键字搜索账号，返回 account id。"""
    keyword = str(name or "").strip()
    if not keyword:
        return None, "账号关键字为空"

    url = api_url.rstrip("/") + "/api/v1/admin/accounts"
    auth_headers_list = _sub2api_auth_headers_list(api_key)

    for auth_headers in auth_headers_list:
        try:
            resp = cffi_requests.get(
                url,
                headers=auth_headers,
                params={
                    "page": 1,
                    "page_size": 20,
                    "platform": "",
                    "type": "",
                    "status": "",
                    "privacy_mode": "",
                    "group": "",
                    "search": keyword,
                    "timezone": "Asia/Shanghai",
                },
                verify=False,
                timeout=20,
                impersonate="chrome110",
            )
            if resp.status_code != 200:
                continue
            items = _extract_items(resp.json())
            if not items:
                return None, "未找到匹配账号"

            low_kw = keyword.lower()
            for item in items:
                nm = str(item.get("name") or "")
                email = str((item.get("credentials") or {}).get("email") or "")
                if nm.lower() == low_kw:
                    return int(item.get("id")), "账号已定位"
                if email and email.split("@", 1)[0].lower() == low_kw:
                    return int(item.get("id")), "账号已定位"
            first = items[0].get("id")
            if first:
                return int(first), "账号已定位(首条匹配)"
        except Exception:
            continue

    return None, "账号搜索失败"


def bulk_update_sub2api_accounts(
    account_ids: list[int],
    api_url: str,
    api_key: str,
    proxy_id: Optional[int] = None,
    group_ids: Optional[list[int]] = None,
) -> Tuple[bool, str]:
    """批量更新 sub2api 账号的代理/分组关联。"""
    ids = [int(x) for x in (account_ids or []) if str(x).strip()]
    if not ids:
        return False, "account_ids 为空"

    payload = {"account_ids": ids}
    if proxy_id is not None:
        payload["proxy_id"] = int(proxy_id)
    if group_ids:
        payload["group_ids"] = [int(x) for x in group_ids]

    if len(payload) == 1:
        return True, "无需更新（未提供 proxy_id/group_ids）"

    url = api_url.rstrip("/") + "/api/v1/admin/accounts/bulk-update"
    auth_headers_list = _sub2api_auth_headers_list(api_key)

    for auth_headers in auth_headers_list:
        try:
            resp = cffi_requests.post(
                url,
                headers={"Content-Type": "application/json", **auth_headers},
                json=payload,
                verify=False,
                timeout=20,
                impersonate="chrome110",
            )
            if resp.status_code not in (200, 201):
                continue
            data = resp.json()
            if isinstance(data, dict) and data.get("code") not in (None, 0):
                continue
            return True, "bulk-update 成功"
        except Exception:
            continue

    return False, "bulk-update 失败"


def generate_token_json(account) -> dict:
    """
    生成 CPA 格式的 Token JSON。
    接受任意 duck-typed 对象（需有 email, access_token, refresh_token 属性），
    expired / account_id 从 JWT 自动解码，与 chatgpt_register 逻辑一致。
    """
    email = getattr(account, "email", "")
    access_token = getattr(account, "access_token", "")
    refresh_token = getattr(account, "refresh_token", "")
    id_token = getattr(account, "id_token", "")
    if access_token and not id_token:
        id_token = _build_compat_id_token(access_token=access_token, email=email)

    expired_str = ""
    account_id = ""
    if access_token:
        payload = _decode_jwt_payload(access_token)
        auth_info = _get_auth_info(payload)
        account_id = auth_info.get("chatgpt_account_id", "")
        exp_timestamp = payload.get("exp")
        if isinstance(exp_timestamp, int) and exp_timestamp > 0:
            exp_dt = datetime.fromtimestamp(
                exp_timestamp, tz=timezone(timedelta(hours=8)))
            expired_str = exp_dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    now = datetime.now(tz=timezone(timedelta(hours=8)))
    return {
        "type": "codex",
        "email": email,
        "expired": expired_str,
        "id_token": id_token,
        "account_id": account_id,
        "access_token": access_token,
        "last_refresh": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "refresh_token": refresh_token,
    }


_DEFAULT_SUB2API_MODEL_MAPPING = {
    "gpt-5-codex-mini": "gpt-5-codex-mini",
    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.1": "gpt-5.1",
    "gpt-5.1-codex": "gpt-5.1-codex",
    "gpt-5-codex": "gpt-5-codex",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.4": "gpt-5.4",
}


def generate_sub2api_export(account, proxy_url: str = "") -> dict:
    """生成 sub2api 格式的导出数据。"""
    email = getattr(account, "email", "")
    access_token = getattr(account, "access_token", "")
    refresh_token = getattr(account, "refresh_token", "")
    id_token = getattr(account, "id_token", "")
    client_id = getattr(account, "client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
    name = getattr(account, "name", email.split("@", 1)[0] if "@" in email else "account")

    if access_token and not id_token:
        id_token = _build_compat_id_token(access_token=access_token, email=email)

    payload = _decode_jwt_payload(access_token) if access_token else {}
    auth_info = _get_auth_info(payload)
    email_from_token = ((payload.get("https://api.openai.com/profile") or {}).get("email") or payload.get("email") or email or "").strip()
    chatgpt_account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    chatgpt_user_id = str(
        auth_info.get("chatgpt_user_id")
        or auth_info.get("user_id")
        or payload.get("sub")
        or ""
    ).strip()
    organization_id = str(auth_info.get("organization_id") or "").strip()
    plan_type = str(auth_info.get("chatgpt_plan_type") or "free").strip() or "free"
    expires_at = payload.get("exp") if isinstance(payload.get("exp"), int) else 0

    credentials = {
        "access_token": access_token,
        "chatgpt_account_id": chatgpt_account_id,
        "chatgpt_user_id": chatgpt_user_id,
        "client_id": client_id,
        "email": email,
        "expires_at": int(expires_at or 0),
        "id_token": id_token,
        "model_mapping": dict(_DEFAULT_SUB2API_MODEL_MAPPING),
        "organization_id": organization_id,
        "plan_type": plan_type,
        "refresh_token": refresh_token,
    }

    account_name = str(name or email.split("@", 1)[0] or "account").strip()
    sub2api_account = {
        "name": account_name,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "email": email,
            "privacy_mode": "training_off",
            "openai_oauth_responses_websockets_v2_enabled": False,
            "openai_oauth_responses_websockets_v2_mode": "off",
        },
        "proxy_key": "",
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }

    proxies = []
    if proxy_url:
        protocol = "http"
        host = ""
        port = 0
        try:
            normalized = proxy_url.strip()
            if "://" in normalized:
                protocol, normalized = normalized.split("://", 1)
            auth, _, hostport = normalized.rpartition("@")
            target = hostport if hostport else auth
            if ":" in target:
                host, port_text = target.rsplit(":", 1)
                port = int(port_text)
            else:
                host = target
            proxy_key = f"{protocol}|{host}|{port}||"
            proxies.append(
                {
                    "proxy_key": proxy_key,
                    "name": "default",
                    "protocol": protocol,
                    "host": host,
                    "port": port,
                    "status": "active",
                }
            )
            sub2api_account["proxy_key"] = proxy_key
        except Exception:
            pass

    return {
        "exported_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxies": proxies,
        "accounts": [sub2api_account],
    }


def upload_to_sub2api(
    account,
    api_url: str = None,
    api_key: str = None,
    import_path: str = None,
    proxy_url: str = "",
    skip_default_group_bind: Any = None,
) -> Tuple[bool, str]:
    """上传单账号到 sub2api（优先 JSON 直传，失败时回退到 multipart file 上传）。"""
    if not api_url:
        api_url = _get_config_value("sub2api_api_url")
    if not api_key:
        api_key = _get_config_value("sub2api_api_key")
    if not import_path:
        import_path = _get_config_value("sub2api_import_path") or "/api/v1/admin/accounts/data"

    api_url = str(api_url or "").strip()
    import_path = "/" + str(import_path or "").strip().lstrip("/")
    if not api_url:
        return False, "sub2api API URL 未配置"

    url = api_url.rstrip("/") + import_path
    payload = generate_sub2api_export(account, proxy_url=proxy_url)
    cfg_skip_default_group_bind = _to_bool(
        _get_config_value("sub2api_skip_default_group_bind"),
        default=False,
    )
    final_skip_default_group_bind = (
        _to_bool(skip_default_group_bind, default=cfg_skip_default_group_bind)
        if skip_default_group_bind is not None
        else cfg_skip_default_group_bind
    )
    request_body = {
        "data": payload,
        "skip_default_group_bind": final_skip_default_group_bind,
    }
    auth_headers_list = [{}]
    if api_key:
        key = str(api_key).strip()
        auth_headers_list = [
            {"X-API-Key": key},
            {"Authorization": f"Bearer {key}"},
            {"Authorization": key},
            {"api-key": key},
        ]

    last_error = "上传失败"
    for auth_headers in auth_headers_list:
        headers = {"Content-Type": "application/json", **auth_headers}
        try:
            resp = cffi_requests.post(
                url,
                headers=headers,
                json=request_body,
                proxies=None,
                verify=False,
                timeout=30,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("code") not in (None, 0):
                        last_error = data.get("message") or f"上传失败: code={data.get('code')}"
                        if "invalid token" in str(last_error).lower():
                            continue
                        return False, str(last_error)
                except Exception:
                    pass
                return True, "上传成功"

            msg = f"上传失败: HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = detail.get("message") or detail.get("error") or msg
            except Exception:
                msg = f"{msg} - {resp.text[:200]}"
            last_error = msg

            low = str(msg).lower()
            if resp.status_code in (401, 403) or "invalid token" in low:
                continue
            if resp.status_code not in (404, 405, 415):
                return False, msg
        except Exception as e:
            last_error = f"上传异常: {e}"
            # 继续尝试下一个鉴权头
            continue

    mime = None
    try:
        file_content = json.dumps(request_body, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"{str(getattr(account, 'email', '') or 'account')}.sub2api.json"
        mime = CurlMime()
        mime.addpart(
            name="file",
            data=file_content,
            filename=filename,
            content_type="application/json",
        )
        for auth_headers in auth_headers_list:
            headers2 = {k: v for k, v in auth_headers.items() if k.lower() != "content-type"}
            resp = cffi_requests.post(
                url,
                multipart=mime,
                headers=headers2,
                proxies=None,
                verify=False,
                timeout=30,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("code") not in (None, 0):
                        msg = data.get("message") or f"上传失败: code={data.get('code')}"
                        if "invalid token" in str(msg).lower():
                            last_error = msg
                            continue
                        return False, msg
                except Exception:
                    pass
                return True, "上传成功"

            msg = f"上传失败: HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = detail.get("message") or detail.get("error") or msg
            except Exception:
                msg = f"{msg} - {resp.text[:200]}"
            last_error = msg

            low = str(msg).lower()
            if resp.status_code in (401, 403) or "invalid token" in low:
                continue
            return False, msg

        return False, str(last_error)
    except Exception as e:
        logger.error(f"sub2api 上传异常: {e}")
        return False, f"上传异常: {e}"
    finally:
        if mime:
            mime.close()


def upload_to_sub2api_with_relations(
    account,
    api_url: str = None,
    api_key: str = None,
    import_path: str = None,
    proxy_url: str = "",
    skip_default_group_bind: Any = None,
    # 新增参数
    proxy_id: Optional[int] = None,
    group_ids: Optional[list[int]] = None,
    auto_create_proxy: bool = True,
) -> Tuple[bool, str]:
    """上传单账号到 sub2api，并设置代理和分组关联。
    
    Args:
        account: 账号对象
        api_url: sub2api 地址
        api_key: API 密钥
        import_path: 导入路径
        proxy_url: 代理URL
        skip_default_group_bind: 是否跳过默认分组绑定
        proxy_id: 代理ID（新增）
        group_ids: 分组ID列表（新增）
        auto_create_proxy: 是否自动创建代理（新增）
    """
    if not api_url:
        api_url = _get_config_value("sub2api_api_url")
    if not api_key:
        api_key = _get_config_value("sub2api_api_key")
    if not import_path:
        import_path = _get_config_value("sub2api_import_path") or "/api/v1/admin/accounts/data"

    api_url = str(api_url or "").strip()
    import_path = "/" + str(import_path or "").strip().lstrip("/")
    if not api_url:
        return False, "sub2api API URL 未配置"

    url = api_url.rstrip("/") + import_path
    payload = generate_sub2api_export(account, proxy_url=proxy_url)
    cfg_skip_default_group_bind = _to_bool(
        _get_config_value("sub2api_skip_default_group_bind"),
        default=False,
    )
    final_skip_default_group_bind = (
        _to_bool(skip_default_group_bind, default=cfg_skip_default_group_bind)
        if skip_default_group_bind is not None
        else cfg_skip_default_group_bind
    )
    request_body = {
        "data": payload,
        "skip_default_group_bind": final_skip_default_group_bind,
    }
    auth_headers_list = [{}]
    if api_key:
        key = str(api_key).strip()
        auth_headers_list = [
            {"X-API-Key": key},
            {"Authorization": f"Bearer {key}"},
            {"Authorization": key},
            {"api-key": key},
        ]

    last_error = "上传失败"
    for auth_headers in auth_headers_list:
        headers = {"Content-Type": "application/json", **auth_headers}
        try:
            resp = cffi_requests.post(
                url,
                headers=headers,
                json=request_body,
                proxies=None,
                verify=False,
                timeout=30,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("code") not in (None, 0):
                        last_error = data.get("message") or f"上传失败: code={data.get('code')}"
                        if "invalid token" in str(last_error).lower():
                            continue
                        return False, str(last_error)
                except Exception:
                    pass
                
                # 上传成功后，设置代理和分组关联
                success_msg = "上传成功"
                
                # 获取账号名称用于后续搜索
                account_name = getattr(account, 'email', '').split('@')[0] or getattr(account, 'name', 'account')
                
                # 如果提供了额外的代理或分组信息，进行关联设置
                if proxy_id or (group_ids and len(group_ids) > 0):
                    # 搜索刚导入的账号
                    found_id, search_msg = search_sub2api_account(account_name, api_url, api_key)
                    if found_id:
                        success, update_msg = bulk_update_sub2api_accounts(
                            account_ids=[found_id],
                            api_url=api_url,
                            api_key=api_key,
                            proxy_id=proxy_id,
                            group_ids=group_ids
                        )
                        success_msg += f"; 关联设置: {update_msg}"
                    else:
                        success_msg += f"; 未能找到刚导入的账号以设置关联: {search_msg}"
                
                # 如果没有提供具体的代理/分组ID，但提供了proxy_url，尝试从proxy_url解析并设置
                elif proxy_url and auto_create_proxy:
                    proxy_key = _build_proxy_key(proxy_url)
                    if proxy_key:
                        # 确保代理存在
                        proxy_id, proxy_msg = ensure_sub2api_proxy(proxy_key, api_url, api_key)
                        if proxy_id:
                            # 搜索刚导入的账号
                            account_name = getattr(account, 'email', '').split('@')[0] or getattr(account, 'name', 'account')
                            found_id, search_msg = search_sub2api_account(account_name, api_url, api_key)
                            if found_id:
                                # 设置代理关联
                                success, update_msg = bulk_update_sub2api_accounts(
                                    account_ids=[found_id],
                                    api_url=api_url,
                                    api_key=api_key,
                                    proxy_id=proxy_id,
                                    group_ids=group_ids
                                )
                                success_msg += f"; 代理关联: {update_msg}"
                            else:
                                success_msg += f"; 未能找到刚导入的账号以设置代理: {search_msg}"
                        else:
                            success_msg += f"; 代理创建失败: {proxy_msg}"
                
                return True, success_msg

            msg = f"上传失败: HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = detail.get("message") or detail.get("error") or msg
            except Exception:
                msg = f"{msg} - {resp.text[:200]}"
            last_error = msg

            low = str(msg).lower()
            if resp.status_code in (401, 403) or "invalid token" in low:
                continue
            if resp.status_code not in (404, 405, 415):
                return False, msg
        except Exception as e:
            last_error = f"上传异常: {e}"
            # 继续尝试下一个鉴权头
            continue

    mime = None
    try:
        file_content = json.dumps(request_body, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"{str(getattr(account, 'email', '') or 'account')}.sub2api.json"
        mime = CurlMime()
        mime.addpart(
            name="file",
            data=file_content,
            filename=filename,
            content_type="application/json",
        )
        for auth_headers in auth_headers_list:
            headers2 = {k: v for k, v in auth_headers.items() if k.lower() != "content-type"}
            resp = cffi_requests.post(
                url,
                multipart=mime,
                headers=headers2,
                proxies=None,
                verify=False,
                timeout=30,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("code") not in (None, 0):
                        msg = data.get("message") or f"上传失败: code={data.get('code')}"
                        if "invalid token" in str(msg).lower():
                            last_error = msg
                            continue
                        return False, msg
                except Exception:
                    pass
                
                # 上传成功后，设置代理和分组关联（同上逻辑）
                success_msg = "上传成功"
                
                # 获取账号名称用于后续搜索
                account_name = getattr(account, 'email', '').split('@')[0] or getattr(account, 'name', 'account')
                
                # 如果提供了额外的代理或分组信息，进行关联设置
                if proxy_id or (group_ids and len(group_ids) > 0):
                    # 搜索刚导入的账号
                    found_id, search_msg = search_sub2api_account(account_name, api_url, api_key)
                    if found_id:
                        success, update_msg = bulk_update_sub2api_accounts(
                            account_ids=[found_id],
                            api_url=api_url,
                            api_key=api_key,
                            proxy_id=proxy_id,
                            group_ids=group_ids
                        )
                        success_msg += f"; 关联设置: {update_msg}"
                    else:
                        success_msg += f"; 未能找到刚导入的账号以设置关联: {search_msg}"
                
                # 如果没有提供具体的代理/分组ID，但提供了proxy_url，尝试从proxy_url解析并设置
                elif proxy_url and auto_create_proxy:
                    proxy_key = _build_proxy_key(proxy_url)
                    if proxy_key:
                        # 确保代理存在
                        proxy_id, proxy_msg = ensure_sub2api_proxy(proxy_key, api_url, api_key)
                        if proxy_id:
                            # 搜索刚导入的账号
                            account_name = getattr(account, 'email', '').split('@')[0] or getattr(account, 'name', 'account')
                            found_id, search_msg = search_sub2api_account(account_name, api_url, api_key)
                            if found_id:
                                # 设置代理关联
                                success, update_msg = bulk_update_sub2api_accounts(
                                    account_ids=[found_id],
                                    api_url=api_url,
                                    api_key=api_key,
                                    proxy_id=proxy_id,
                                    group_ids=group_ids
                                )
                                success_msg += f"; 代理关联: {update_msg}"
                            else:
                                success_msg += f"; 未能找到刚导入的账号以设置代理: {search_msg}"
                        else:
                            success_msg += f"; 代理创建失败: {proxy_msg}"
                
                return True, success_msg

            msg = f"上传失败: HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = detail.get("message") or detail.get("error") or msg
            except Exception:
                msg = f"{msg} - {resp.text[:200]}"
            last_error = msg

            low = str(msg).lower()
            if resp.status_code in (401, 403) or "invalid token" in low:
                continue
            return False, msg

        return False, str(last_error)
    except Exception as e:
        logger.error(f"sub2api 上传异常: {e}")
        return False, f"上传异常: {e}"
    finally:
        if mime:
            mime.close()


def upload_to_cpa(
    token_data: dict,
    api_url: str = None,
    api_key: str = None,
    proxy: str = None,
) -> Tuple[bool, str]:
    """上传单个账号到 CPA 管理平台（不走代理）。
    api_url / api_key 为空时自动从 ConfigStore 读取。"""
    if not api_url:
        api_url = _get_config_value("cpa_api_url")
    if not api_key:
        api_key = _get_config_value("cpa_api_key")
    if not api_url:
        return False, "CPA API URL 未配置"

    upload_url = f"{api_url.rstrip('/')}/v0/management/auth-files"

    filename = f"{token_data['email']}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key or ''}",
    }

    mime = None
    try:
        mime = CurlMime()
        mime.addpart(
            name="file",
            data=file_content,
            filename=filename,
            content_type="application/json",
        )

        response = cffi_requests.post(
            upload_url,
            multipart=mime,
            headers=headers,
            proxies=None,
            verify=False,
            timeout=30,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201):
            return True, "上传成功"

        error_msg = f"上传失败: HTTP {response.status_code}"
        try:
            error_detail = response.json()
            if isinstance(error_detail, dict):
                error_msg = error_detail.get("message") or error_msg
        except Exception:
            error_msg = f"{error_msg} - {response.text[:200]}"

        return False, error_msg

    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
    finally:
        if mime:
            mime.close()
