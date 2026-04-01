#!/usr/bin/env python3
"""周期触发注册任务（避免并发重复提交）。"""
import json
import os
import sys
import fcntl
import urllib.request
import urllib.error
from typing import Any, Dict


def _request_json(method: str, url: str, payload: Dict[str, Any] | None = None, timeout: int = 20) -> Any:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore").strip()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} {method} {url} 失败: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"请求失败: {method} {url}, error={e}") from e


def _parse_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return int(raw)
    except Exception as e:
        raise RuntimeError(f"环境变量 {name} 不是合法整数: {raw}") from e


def _parse_float_env(name: str, default: float) -> float:
    raw = str(os.getenv(name, str(default))).strip()
    try:
        return float(raw)
    except Exception as e:
        raise RuntimeError(f"环境变量 {name} 不是合法浮点数: {raw}") from e


def _load_extra_json() -> dict:
    raw = str(os.getenv("CRON_EXTRA_JSON", "{}")).strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"CRON_EXTRA_JSON 不是合法 JSON: {raw}") from e
    if not isinstance(val, dict):
        raise RuntimeError("CRON_EXTRA_JSON 必须是 JSON 对象")
    return val


def _is_task_active(task: Any) -> bool:
    if not isinstance(task, dict):
        return False
    status = str(task.get("status") or "").strip().lower()
    return status in {"pending", "running"}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _build_payload() -> dict:
    payload = {
        "platform": str(os.getenv("CRON_PLATFORM", "chatgpt")).strip() or "chatgpt",
        "count": _parse_int_env("CRON_COUNT", 99),
        "concurrency": _parse_int_env("CRON_CONCURRENCY", 1),
        "register_delay_seconds": _parse_float_env("CRON_REGISTER_DELAY_SECONDS", 0),
        "proxy": (str(os.getenv("CRON_PROXY", "")).strip() or None),
        "executor_type": str(os.getenv("CRON_EXECUTOR_TYPE", "protocol")).strip() or "protocol",
        "captcha_solver": str(os.getenv("CRON_CAPTCHA_SOLVER", "yescaptcha")).strip() or "yescaptcha",
        "extra": _load_extra_json(),
    }

    fixed_email = str(os.getenv("CRON_EMAIL", "")).strip()
    fixed_password = str(os.getenv("CRON_PASSWORD", "")).strip()
    if fixed_email:
        payload["email"] = fixed_email
    if fixed_password:
        payload["password"] = fixed_password

    if payload["count"] <= 0:
        raise RuntimeError("CRON_COUNT 必须 > 0")
    if payload["concurrency"] <= 0:
        raise RuntimeError("CRON_CONCURRENCY 必须 > 0")

    return payload


def main() -> int:
    base_url = str(os.getenv("CRON_API_BASE", "http://127.0.0.1:8000")).strip().rstrip("/")
    if not base_url:
        raise RuntimeError("CRON_API_BASE 不能为空")

    tasks_url = f"{base_url}/api/tasks"
    register_url = f"{base_url}/api/tasks/register"

    lock_path = str(os.getenv("CRON_LOCK_FILE", "/tmp/any-auto-register-cron.lock")).strip() or "/tmp/any-auto-register-cron.lock"
    lock_dir = os.path.dirname(lock_path) or "."
    os.makedirs(lock_dir, exist_ok=True)

    with open(lock_path, "w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[cron-register] 上一次调度仍在执行，跳过本轮")
            return 0

        tasks = _request_json("GET", tasks_url)
        if not isinstance(tasks, list):
            raise RuntimeError(f"/api/tasks 返回结构异常: {tasks}")

        active_count = sum(1 for t in tasks if _is_task_active(t))
        if active_count > 0:
            print(f"[cron-register] 检测到 {active_count} 个运行中任务，跳过本轮")
            return 0

        payload = _build_payload()
        if _bool_env("CRON_DRY_RUN", default=False):
            print("[cron-register] DRY_RUN=1，不提交任务")
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        result = _request_json("POST", register_url, payload=payload)
        task_id = ""
        if isinstance(result, dict):
            task_id = str(result.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError(f"提交任务失败，返回: {result}")

        print(
            "[cron-register] 已提交任务: "
            f"task_id={task_id}, platform={payload['platform']}, "
            f"count={payload['count']}, concurrency={payload['concurrency']}"
        )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[cron-register] 异常: {e}")
        raise SystemExit(1)
