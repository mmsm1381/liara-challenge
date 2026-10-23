"""The only module that talks to Liara.

Typed methods return normalized plain dicts/lists. There is deliberately no
"call any URL" method. Changes (POST/PATCH/DELETE) are never retried.
Endpoints are marked in ``API_NOTES.md`` as spec-backed or UNVERIFIED.

``FakeLiaraClient`` at the bottom imitates the API for demos and tests; its
state lives in a JSON file shared by the web process and the RQ worker.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import fnmatch
import json
import logging
import os
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import requests
from django.conf import settings

from assistant.redact import redact, redact_obj

logger = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_LOG_LINES = 200
UPLOAD_TIMEOUT = 300

# --------------------------------------------------------------------------- errors

_DEFAULT_FA = {
    "unauthorized": "این کلید معتبر نیست یا منقضی شده.",
    "not_found": "این مورد در حساب لیارا پیدا نشد.",
    "frozen": "موجودی حساب لیارا کافی نیست؛ حساب را در کنسول لیارا شارژ کنید.",
    "rate_limited": "تعداد درخواست‌ها به لیارا زیاد بود؛ چند لحظه بعد دوباره امتحان کنید.",
    "unavailable": "لیارا الان پاسخ نمی‌دهد؛ یک دقیقه دیگر دوباره امتحان کنید.",
    "timeout": "لیارا دیر جواب داد؛ یک دقیقه دیگر دوباره امتحان کنید.",
    "bad_request": "لیارا این درخواست را نپذیرفت.",
    "unknown": "پاسخ غیرمنتظره‌ای از لیارا آمد.",
}


class LiaraError(Exception):
    """A failed Liara call. ``message_fa`` is safe to show to the user."""

    def __init__(self, code: str, status: int | None = None, message_fa: str | None = None):
        self.code = code
        self.status = status
        self.message_fa = message_fa or _DEFAULT_FA.get(code, _DEFAULT_FA["unknown"])
        super().__init__(f"{code}: {self.message_fa}")


def _raise_for_response(resp: requests.Response, token: str) -> None:
    """Translate a non-2xx response into a LiaraError (body is redacted)."""
    status = resp.status_code
    text = (resp.text or "")[:2000]
    if status in (401, 403):
        raise LiaraError("unauthorized", status)
    if status == 404:
        raise LiaraError("not_found", status)
    # 402 "Not enough balance." is live-verified (2026-10-23) on create calls.
    if status == 402 or (status == 400 and "frozen_project" in text):
        raise LiaraError("frozen", status)
    if status == 429:
        raise LiaraError("rate_limited", status)
    if status >= 500:
        raise LiaraError("unavailable", status)
    if status >= 400:
        message = ""
        with contextlib.suppress(ValueError):
            body = resp.json()
            if isinstance(body, dict):
                message = str(body.get("message") or body.get("error") or "")
        message = message or text
        if token:
            message = message.replace(token, "[REDACTED]")
        message = redact(message)[:200]
        if status == 409:
            fa = "این نام یا مورد قبلاً در لیارا وجود دارد."
        else:
            fa = _DEFAULT_FA["bad_request"]
        if message:
            fa = f"{fa} پیام لیارا: {message}"
        raise LiaraError("bad_request", status, fa)
    raise LiaraError("unknown", status)  # 1xx/3xx: we never follow redirects


# --------------------------------------------------------------------------- helpers

def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _iso(value: Any) -> str:
    return str(value) if value is not None else ""


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _region_domain(base_url: str, network: Any) -> str:
    """Default app domain, derived the way the CLI does (iran region and no private network)."""
    return ".iran.liara.run" if "iran" in base_url and not network else ".liara.run"


# --------------------------------------------------------------------------- real client

class LiaraClient:
    """Typed client for the Liara PaaS, DBaaS and object-storage APIs."""

    def __init__(
        self,
        token: str,
        team_id: str = "",
        base_url: str | None = None,
        storage_base_url: str | None = None,
        timeout: float | None = None,
    ):
        self._token = token
        self.team_id = team_id or ""
        self.base_url = (base_url or settings.LIARA_API_BASE).rstrip("/")
        self.storage_base_url = (storage_base_url or settings.LIARA_STORAGE_API_BASE).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.LIARA_TIMEOUT
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        # Status/keys of recent calls, used by the probe command. Never holds values.
        self.calls: list[dict] = []

    def __repr__(self) -> str:  # never leak the token
        return f"<LiaraClient base={self.base_url} team={self.team_id or '-'}>"

    # ---- transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        files: dict | None = None,
        storage: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """One HTTP call, no retry. Returns parsed JSON (or {} for empty bodies)."""
        base = self.storage_base_url if storage else self.base_url
        query = dict(params or {})
        if self.team_id:
            query["teamID"] = self.team_id
        try:
            resp = self.session.request(
                method,
                base + path,
                params=query or None,
                json=json_body,
                files=files,
                timeout=timeout or self.timeout,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise LiaraError("timeout") from None
        except requests.RequestException:
            raise LiaraError("unavailable") from None
        logger.debug("liara %s %s -> %s", method, path, resp.status_code)
        data: Any = {}
        if resp.content:
            with contextlib.suppress(ValueError):
                data = resp.json()
        self.calls.append(
            {
                "method": method,
                "path": path,
                "status": resp.status_code,
                "keys": sorted(data.keys()) if isinstance(data, dict) else (["<list>"] if isinstance(data, list) else []),
            }
        )
        del self.calls[:-50]
        if not 200 <= resp.status_code < 300:
            _raise_for_response(resp, self._token)
        return data

    # ---- apps (spec: paas.yaml)

    def list_projects(self) -> list[dict]:
        """GET /v1/projects -> [{name, platform, plan_id, status, created_at}]."""
        data = self._request("GET", "/v1/projects")
        return [
            {
                "name": p.get("project_id", ""),
                "platform": p.get("type", ""),
                "plan_id": p.get("planID", ""),
                "status": p.get("status", ""),
                "created_at": _iso(p.get("created_at")),
            }
            for p in _as_list(data.get("projects") if isinstance(data, dict) else None)
            if isinstance(p, dict)
        ]

    def get_project(self, name: str) -> dict:
        """GET /v1/projects/{name}. Env var *names* only, never values."""
        data = self._request("GET", f"/v1/projects/{name}")
        p = data.get("project", {}) if isinstance(data, dict) else {}
        envs = [e.get("key") for e in _as_list(p.get("envs")) if isinstance(e, dict) and e.get("key")]
        out = {
            "name": p.get("project_id", name),
            "platform": p.get("type", ""),
            "plan_id": p.get("planID", ""),
            "status": p.get("status", ""),
            "scale": p.get("scale"),
            "is_deployed": p.get("isDeployed"),
            "envs_keys": envs,
        }
        if p.get("defaultSubdomain", True):
            # Derived like the CLI does; the spec has no `url` field.
            out["url"] = f"https://{out['name']}{_region_domain(self.base_url, p.get('network'))}"
        return out

    def create_project(self, name: str, platform: str, plan_id: str, network: str | None = None) -> dict:
        """POST /v1/projects {name, planID, platform, bundlePlanID?, network?}."""
        body = {"name": name, "planID": plan_id, "platform": platform, **_bundle(plan_id)}
        if network:
            body["network"] = network
        self._request("POST", "/v1/projects", json_body=body)
        return {"name": name, "platform": platform, "plan_id": plan_id}

    def get_logs(self, name: str, since: int | None = None) -> list[dict]:
        """GET /v1/projects/{name}/logs?since= -> last 200 [{ts, message}]. `since` is unix seconds (unit unverified)."""
        since = int(since if since is not None else time.time() - 3600)
        data = self._request("GET", f"/v1/projects/{name}/logs", params={"since": since})
        rows = data if isinstance(data, list) else _as_list(data.get("logs") if isinstance(data, dict) else None)
        out = [
            {"ts": _iso(r.get("datetime")), "message": str(r.get("message", ""))}
            for r in rows
            if isinstance(r, dict)
        ]
        return out[-MAX_LOG_LINES:]

    def list_releases(self, name: str) -> dict:
        """GET /v1/projects/{name}/releases -> {current, releases:[{id, state, created_at, source_id?}]}."""
        data = self._request("GET", f"/v1/projects/{name}/releases", params={"page": 1, "count": 10})
        data = data if isinstance(data, dict) else {}
        releases = []
        for r in _as_list(data.get("releases")):
            if not isinstance(r, dict):
                continue
            item = {
                "id": r.get("_id") or r.get("releaseID") or r.get("id") or "",
                "state": r.get("state") or r.get("status") or "",
                "created_at": _iso(r.get("createdAt")),
                "platform": r.get("projectType") or r.get("type") or "",
                "port": r.get("port"),
                "source_available": r.get("sourceAvailable"),
            }
            # UNVERIFIED: the spec lists no source id on a release, only `sourceAvailable`.
            source_id = r.get("sourceID") or r.get("sourceId") or r.get("source_id")
            if source_id:
                item["source_id"] = source_id
            releases.append(item)
        return {"current": data.get("currentRelease") or "", "releases": releases}

    def get_metrics_summary(self, name: str) -> dict:
        """GET /v1/projects/{name}/metrics/summary. Raw numbers; missing keys stay missing."""
        data = self._request("GET", f"/v1/projects/{name}/metrics/summary")
        if not isinstance(data, dict):
            return {}
        return {k: data[k] for k in ("cpuUsage", "memoryUsage", "disksUsage") if k in data}

    def restart(self, name: str) -> None:
        """POST /v1/projects/{name}/actions/restart."""
        self._request("POST", f"/v1/projects/{name}/actions/restart")

    def resize(self, name: str, plan_id: str) -> None:
        """POST /v1/projects/{name}/resize {planID}."""
        self._request("POST", f"/v1/projects/{name}/resize", json_body={"planID": plan_id})

    def update_envs(self, name: str, variables: dict[str, str], merge: bool = True) -> None:
        """POST /v1/projects/update-envs {project, variables:[{key,value}]}.

        UNVERIFIED: whether the list replaces all envs. With ``merge`` (default) existing
        envs are re-sent so nothing is dropped, which is also what the CLI's env:set does.
        """
        merged: dict[str, str] = {}
        if merge:
            data = self._request("GET", f"/v1/projects/{name}")
            project = data.get("project", {}) if isinstance(data, dict) else {}
            for e in _as_list(project.get("envs")):
                if isinstance(e, dict) and e.get("key") is not None:
                    merged[e["key"]] = e.get("value", "")
        merged.update({str(k): str(v) for k, v in variables.items()})
        body = {"project": name, "variables": [{"key": k, "value": v} for k, v in merged.items()]}
        self._request("POST", "/v1/projects/update-envs", json_body=body)

    # ---- deploy (spec: paas.yaml; build-logs/cancel from the CLI only)

    def upload_source(self, name: str, archive_path: str | Path) -> str:
        """POST /v2/projects/{name}/sources (multipart `file`) -> sourceID."""
        path = Path(archive_path)
        with path.open("rb") as fh:
            data = self._request(
                "POST",
                f"/v2/projects/{name}/sources",
                files={"file": (path.name, fh, "application/gzip")},
                timeout=max(self.timeout, UPLOAD_TIMEOUT),
            )
        source_id = data.get("sourceID") if isinstance(data, dict) else None
        if not source_id:
            raise LiaraError("unknown", message_fa="لیارا شناسه‌ی سورس آپلودشده را برنگرداند.")
        return source_id

    def create_release(
        self,
        name: str,
        source_id: str,
        platform: str,
        port: int,
        message: str = "",
        build_args: dict[str, str] | None = None,
        health_check: dict | None = None,
    ) -> str:
        """POST /v2/projects/{name}/releases -> releaseID. Field names follow the CLI's deploy.ts."""
        body: dict[str, Any] = {
            "build": {"cache": True, "args": build_args or {}},
            "port": port,
            "type": platform,
            "sourceID": source_id,
            "platformConfig": {},
        }
        if message:
            body["message"] = message
        if health_check:
            body["healthCheck"] = health_check
        data = self._request("POST", f"/v2/projects/{name}/releases", json_body=body)
        release_id = data.get("releaseID") if isinstance(data, dict) else None
        if not release_id:
            raise LiaraError("unknown", message_fa="لیارا شناسه‌ی نسخه‌ی جدید را برنگرداند.")
        return release_id

    def get_build_logs(self, release_id: str, since: int | None = None) -> dict:
        """GET /v2/releases/{id}/build-logs?since= (CLI only; response shape partly UNVERIFIED).

        Returns {state, fail_reason, lines, next_since}. States: DEPLOYING/READY/FAILED/TIMEOUT/CANCELED.
        """
        params = {"since": since} if since is not None else {}
        data = self._request("GET", f"/v2/releases/{release_id}/build-logs", params=params)
        data = data if isinstance(data, dict) else {}
        release = data.get("release") if isinstance(data.get("release"), dict) else {}
        lines: list[str] = []
        stamps: list[float] = []
        for item in _as_list(data.get("buildOutput")):
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict):
                text = item.get("line") or item.get("message") or ""
                if text:
                    lines.append(str(text))
                stamp = _num(item.get("timestamp") or item.get("time"))
                if stamp is not None:
                    stamps.append(stamp)
        next_since = int(max(stamps)) if stamps else (since if since is not None else None)
        return {
            "state": release.get("state") or data.get("state") or "",
            "fail_reason": release.get("failReason") or data.get("failReason") or "",
            "lines": [redact(line)[:1000] for line in lines][-MAX_LOG_LINES:],
            "next_since": next_since,
        }

    def cancel_build(self, release_id: str) -> None:
        """POST /v2/releases/{id}/cancel (CLI only)."""
        self._request("POST", f"/v2/releases/{release_id}/cancel")

    def rollback(self, name: str, release_id: str) -> str:
        """UNVERIFIED: no rollback endpoint exists. Re-release an older release's source.

        Works only if list_releases exposes that release's source_id; otherwise bad_request.
        """
        info = self.list_releases(name)
        target = next((r for r in info["releases"] if r["id"] == release_id), None)
        if target is None:
            raise LiaraError("not_found", 404)
        if not target.get("source_id"):
            raise LiaraError(
                "bad_request",
                message_fa="لیارا شناسه‌ی سورس این نسخه را نمی‌دهد، پس برگرداندن خودکار ممکن نیست؛ از کنسول لیارا نسخه را برگردانید.",
            )
        return self.create_release(
            name,
            target["source_id"],
            target.get("platform") or self.get_project(name)["platform"],
            int(target.get("port") or 80),
            message=f"rollback to {release_id}",
        )

    # ---- networks (live-checked 2026-10-23: without `network`, creating a g2 app
    # answers g2_plan_unavailable on accounts that use private networks)

    def list_networks(self) -> list[dict]:
        """GET /v1/networks -> [{id, name}]."""
        data = self._request("GET", "/v1/networks")
        return [
            {"id": n.get("_id", ""), "name": n.get("name", "")}
            for n in _as_list(data.get("networks") if isinstance(data, dict) else None)
            if isinstance(n, dict)
        ]

    def create_network(self, name: str) -> None:
        """POST /v1/networks {networkName} (Liara CLI network:create)."""
        self._request("POST", "/v1/networks", json_body={"networkName": name})

    # ---- plans (live-verified 2026-10-23: there is no /v1/plans; plans come from /v1/me)

    def list_plans(self) -> list[dict]:
        """GET /v1/me -> plans, the same source the Liara CLI uses.

        -> [{id, kind: "app"|"database", ram_mb, cpu, disk_gb, price_month_toman}].
        """
        data = self._request("GET", "/v1/me")
        return _normalize_plans(data.get("plans") if isinstance(data, dict) else None)

    # ---- databases (spec: spec_dbaas.yaml)

    def list_databases(self) -> list[dict]:
        """GET /v1/databases -> [{id, type, status, plan_id, host}]."""
        data = self._request("GET", "/v1/databases")
        return [
            {
                "id": d.get("id") or d.get("DBId") or "",
                "type": d.get("type", ""),
                "status": d.get("status", ""),
                "plan_id": d.get("planID", ""),
                "host": d.get("hostname", ""),
            }
            for d in _as_list(data.get("databases") if isinstance(data, dict) else None)
            if isinstance(d, dict)
        ]

    def create_database(self, type: str, name: str, plan_id: str, version: str | None = None, network: str | None = None) -> dict:
        """POST /v1/databases {type, hostname, planID, bundlePlanID?, version?, network?, publicNetwork} -> {id}."""
        body: dict[str, Any] = {
            "type": _DB_TYPES.get(type, type), "hostname": name, "planID": plan_id, "publicNetwork": False, **_bundle(plan_id),
        }
        if version:
            body["version"] = version
        if network:
            body["network"] = network
        data = self._request("POST", "/v1/databases", json_body=body)
        db_id = data.get("databaseID") if isinstance(data, dict) else None
        if not db_id:
            raise LiaraError("unknown", message_fa="لیارا شناسه‌ی دیتابیس را برنگرداند.")
        return {"id": db_id}

    def get_database(self, db_id: str) -> dict:
        """GET /v1/databases/{id}. The password goes to the caller only, never into logs."""
        data = self._request("GET", f"/v1/databases/{db_id}")
        d = data.get("database", {}) if isinstance(data, dict) else {}
        node = d.get("node") if isinstance(d.get("node"), dict) else {}
        return {
            "id": d.get("id") or d.get("DBId") or db_id,
            "type": d.get("type", ""),
            "status": d.get("status", ""),
            "host": node.get("host") or d.get("hostname", ""),
            "port": d.get("port") or d.get("internalPort"),
            "internal_host": d.get("hostname", ""),  # UNVERIFIED: assumed in-network DNS name
            "internal_port": d.get("internalPort"),
            "username": d.get("username", ""),
            "password": d.get("root_password", ""),
            "db_name": d.get("dbName", ""),
        }

    # ---- object storage (spec: spec_object-storage.yaml, other base URL)

    def list_buckets(self) -> list[dict]:
        """GET /api/v1/buckets -> [{id, name, plan, permission, status}]."""
        data = self._request("GET", "/api/v1/buckets", storage=True)
        return [
            {k: b.get(k, "") for k in ("id", "name", "plan", "permission", "status")}
            for b in _as_list(data.get("buckets") if isinstance(data, dict) else None)
            if isinstance(b, dict)
        ]

    def create_bucket(self, name: str, plan: str, permission: str = "private") -> dict:
        """POST /api/v1/buckets {name, plan, permission}. Plans: 20g/40g/80g/160g."""
        self._request("POST", "/api/v1/buckets", json_body={"name": name, "plan": plan, "permission": permission}, storage=True)
        return {"name": name, "plan": plan, "permission": permission}

    def create_bucket_key(self, name: str) -> dict:
        """POST /api/v1/keys scoped to one bucket -> {access_key, secret_key}.

        UNVERIFIED: the spec shows no response body for create; secretKey is assumed
        to be returned like in PATCH /keys/{key}/revoke.
        """
        bucket = self._request("GET", f"/api/v1/buckets/{name}", storage=True)
        info = bucket.get("bucket", {}) if isinstance(bucket, dict) else {}
        body = {"description": f"liara-assistant {name}", "buckets": [{"id": info.get("id", ""), "name": name}]}
        data = self._request("POST", "/api/v1/keys", json_body=body, storage=True)
        data = data if isinstance(data, dict) else {}
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        access = inner.get("accessKey") or inner.get("access_key")
        secret = inner.get("secretKey") or inner.get("secret_key")
        if not (access and secret):
            raise LiaraError("unknown", message_fa="کلید دسترسی ساخته شد اما لیارا مقدار محرمانه‌ی آن را برنگرداند؛ از کنسول لیارا آن را بسازید.")
        return {"access_key": access, "secret_key": secret}

    # NOTE: no get_me()/balance method: none of the public specs has an account or balance endpoint.


# Live-verified 2026-10-23: POST /v1/databases accepts "postgres", not "postgresql".
_DB_TYPES = {"postgresql": "postgres"}


def _bundle(plan_id: str) -> dict:
    """Feature bundle for a plan. The Liara CLI sends `bundlePlanID` for g2 plans.

    In /v1/me, a g2 plan's monthly price equals its "standard" bundle price, so
    "standard" keeps the cost the user approved. Not live-verified: a create call
    builds a paid resource.
    """
    return {"bundlePlanID": "standard"} if plan_id.endswith("-g2") else {}


def _normalize_plans(plans: Any) -> list[dict]:
    """Normalize `plans` from GET /v1/me.

    `plans.projects` and `plans.databases` map plan ID -> {available, price, volume,
    RAM: {amount}, CPU: {amount}, region?, displayPrice?}. `displayPrice` is toman per
    month; `price` is toman per hour (the CLI shows price * 720 per month). Like the CLI, only available current-generation ("-g2")
    plans are returned, and app plans must be in the "iran" region.
    """
    if not isinstance(plans, dict):
        return []
    out = []
    for section, kind in (("projects", "app"), ("databases", "database")):
        items = plans.get(section)
        if not isinstance(items, dict):
            continue
        for plan_id, p in items.items():
            if not isinstance(p, dict) or not p.get("available") or "g2" not in str(plan_id):
                continue
            if kind == "app" and p.get("region") != "iran":
                continue
            ram = _num((p.get("RAM") or {}).get("amount"))
            hourly = _num(p.get("price"))
            monthly = _num(p.get("displayPrice"))
            if monthly is None and hourly is not None:
                monthly = hourly * 720
            out.append(
                {
                    "id": plan_id,
                    "kind": kind,
                    "ram_mb": int(ram * 1024) if ram is not None else None,
                    "cpu": _num((p.get("CPU") or {}).get("amount")),
                    "disk_gb": _num(p.get("volume")),
                    "price_month_toman": round(monthly) if monthly is not None else None,
                }
            )
    return out


# --------------------------------------------------------------------------- URL check

def check_url(url: str, timeout: float = 10) -> dict:
    """GET a URL (at most 3 redirects). Never raises -> {ok, status_code, ms, error?}."""
    if settings.LIARA_FAKE:
        return fake_check_url(url)
    started = time.monotonic()
    result: dict[str, Any] = {"ok": False, "status_code": None, "ms": 0}
    try:
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("bad scheme")
        with requests.Session() as s:
            s.max_redirects = 3
            resp = s.get(url, timeout=timeout, allow_redirects=True, stream=True)
            resp.close()
        result["status_code"] = resp.status_code
        result["ok"] = resp.status_code < 400
    except requests.Timeout:
        result["error"] = "timeout"
    except requests.TooManyRedirects:
        result["error"] = "too_many_redirects"
    except requests.RequestException:
        result["error"] = "connection_error"
    except Exception:  # noqa: BLE001 - contract: never raises
        result["error"] = "invalid_url"
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


# --------------------------------------------------------------------------- archive

_IGNORE_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".idea", ".vscode", ".pytest_cache", ".mypy_cache"}
_IGNORE_FILES = {".DS_Store", ".liaraignore_cache"}
_ENV_ALLOWED = {".env.example", ".env.sample", ".env.template"}


def _load_ignore_patterns(src: Path) -> list[str]:
    """Simple .gitignore/.liaraignore support (globs only, no negation)."""
    patterns: list[str] = []
    for name in (".gitignore", ".liaraignore"):
        f = src / name
        if f.is_file():
            for line in f.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "!")):
                    patterns.append(line.strip("/"))
    return patterns


def _ignored(rel: str, name: str, is_dir: bool, patterns: list[str]) -> bool:
    if is_dir and name in _IGNORE_DIRS:
        return True
    if not is_dir:
        if name in _IGNORE_FILES or name.endswith(".pyc"):
            return True
        if name == ".env" or (name.startswith(".env.") and name not in _ENV_ALLOWED):
            return True
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def build_archive(src_dir: str | Path, dest_path: str | Path) -> int:
    """Create a .tar.gz of ``src_dir`` like the CLI does; return its size in bytes.

    Skips secrets (.env), VCS data and dependency folders. Raises LiaraError(bad_request) over 200 MB.
    """
    src = Path(src_dir).resolve()
    dest = Path(dest_path)
    patterns = _load_ignore_patterns(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for root, dirs, files in os.walk(src):
            root_p = Path(root)
            dirs[:] = sorted(
                d for d in dirs
                if not (root_p / d).is_symlink() and not _ignored((root_p / d).relative_to(src).as_posix(), d, True, patterns)
            )
            for fname in sorted(files):
                full = root_p / fname
                rel = full.relative_to(src).as_posix()
                if full.is_symlink() or _ignored(rel, fname, False, patterns) or full.resolve() == dest.resolve():
                    continue
                tar.add(full, arcname=rel, recursive=False)
    size = dest.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        dest.unlink(missing_ok=True)
        raise LiaraError("bad_request", message_fa="حجم پروژه بعد از فشرده‌سازی بیشتر از ۲۰۰ مگابایت است؛ فایل‌های اضافه (مثل node_modules) را حذف کنید.")
    return size


# --------------------------------------------------------------------------- fake client

# FAKE DATA: plans and prices below are invented for demos, not real Liara pricing.
_FAKE_PLANS = [
    {"id": "fake-app-small", "kind": "app", "ram_mb": 512, "cpu": 0.5, "disk_gb": 1, "price_month_toman": 150_000},
    {"id": "fake-app-medium", "kind": "app", "ram_mb": 1024, "cpu": 1, "disk_gb": 2, "price_month_toman": 290_000},
    {"id": "fake-app-large", "kind": "app", "ram_mb": 2048, "cpu": 2, "disk_gb": 5, "price_month_toman": 560_000},
    {"id": "fake-db-small", "kind": "database", "ram_mb": 1024, "cpu": 1, "disk_gb": 5, "price_month_toman": 320_000},
    {"id": "fake-db-medium", "kind": "database", "ram_mb": 2048, "cpu": 2, "disk_gb": 10, "price_month_toman": 610_000},
]
_BROKEN = {"missing_package", "oom", "missing_env"}
SCENARIOS = _BROKEN | {"healthy"}
_FAKE_BUCKET_PLANS = {"20g", "40g", "80g", "160g"}


def _fake_path() -> Path:
    return Path(settings.BASE_DIR) / "var" / "fake_liara.json"


def _now_iso(delta_s: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seed() -> dict:
    def app(name: str, platform: str, plan: str, port: int) -> dict:
        return {
            "name": name, "platform": platform, "plan_id": plan, "status": "RUNNING",
            "created_at": _now_iso(-86400 * 20), "envs": {"NODE_ENV": "production"} if platform == "node" else {},
            "scenario": "healthy", "port": port, "current": f"rel-{name}-2",
            "releases": [
                {"id": f"rel-{name}-1", "state": "READY", "created_at": _now_iso(-86400 * 9), "source_id": f"src-{name}-1", "platform": platform, "port": port},
                {"id": f"rel-{name}-2", "state": "READY", "created_at": _now_iso(-86400 * 3), "source_id": f"src-{name}-2", "platform": platform, "port": port},
            ],
        }
    return {
        "apps": {"my-shop": app("my-shop", "node", "fake-app-small", 3000), "blog": app("blog", "static", "fake-app-small", 80)},
        "sources": {}, "builds": {}, "dbs": {}, "buckets": {}, "keys": [],
    }


@contextlib.contextmanager
def _fake_state() -> Iterator[dict]:
    """Locked read-modify-write of the shared JSON state (atomic replace on save)."""
    path = _fake_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text()) if path.exists() else _seed()
        except ValueError:
            state = _seed()
        yield state
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)


def fake_reset() -> None:
    """Delete the fake state (back to the two seed apps)."""
    _fake_path().unlink(missing_ok=True)


def fake_set_scenario(app: str, scenario: str) -> None:
    """Put a fake app into a broken/healthy scenario: missing_package, oom, missing_env, healthy."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    with _fake_state() as st:
        a = st["apps"].get(app)
        if a is None:
            raise LiaraError("not_found", 404)
        a["scenario"] = scenario
        a["status"] = "RUNNING" if scenario == "healthy" else "CRASH_LOOP"
        a["scenario_since"] = time.time()
        if scenario == "missing_package":
            # The crash comes from a newly deployed release, like in the demo script.
            prev = a["releases"][-1] if a["releases"] else {}
            rel_id = f"rel-{app}-{len(a['releases']) + 1}"
            a["releases"].append({"id": rel_id, "state": "READY", "created_at": _now_iso(), "source_id": f"src-{rel_id}",
                                  "platform": prev.get("platform", a["platform"]), "port": prev.get("port", a["port"])})
            a["current"] = rel_id


def fake_check_url(url: str) -> dict:
    """200 for fake apps unless their scenario is broken (then 502)."""
    host = (urlparse(url).hostname or "").split(".")[0]
    with _fake_state() as st:
        a = st["apps"].get(host)
        broken = bool(a) and a["scenario"] in _BROKEN
    code = 502 if broken else 200
    result: dict[str, Any] = {"ok": not broken, "status_code": code, "ms": 42}
    if broken:
        result["error"] = "bad_gateway"
    return result


class FakeLiaraClient:
    """In-memory-ish Liara for demos/tests. Same methods as LiaraClient; no network."""

    def __init__(self, token: str = "fake", team_id: str = "", **_: Any):
        self._token = token
        self.team_id = team_id
        self.calls: list[dict] = []

    def __repr__(self) -> str:
        return "<FakeLiaraClient>"

    def _auth(self, label: str) -> None:
        if self._token == "fake-invalid" or self._token.startswith("bad"):
            raise LiaraError("unauthorized", 401)
        self.calls.append({"method": "FAKE", "path": label, "status": 200, "keys": []})
        del self.calls[:-50]

    @staticmethod
    def _app(st: dict, name: str) -> dict:
        a = st["apps"].get(name)
        if a is None:
            raise LiaraError("not_found", 404)
        return a

    @staticmethod
    def _plan(plan_id: str) -> dict:
        return next((p for p in _FAKE_PLANS if p["id"] == plan_id), _FAKE_PLANS[0])

    # ---- apps

    def list_projects(self) -> list[dict]:
        self._auth("list_projects")
        with _fake_state() as st:
            return [{k: a[k] for k in ("name", "platform", "plan_id", "status", "created_at")} for a in st["apps"].values()]

    def get_project(self, name: str) -> dict:
        self._auth("get_project")
        with _fake_state() as st:
            a = self._app(st, name)
            return {
                "name": name, "platform": a["platform"], "plan_id": a["plan_id"], "status": a["status"],
                "scale": 1, "is_deployed": bool(a["releases"]),
                "url": f"https://{name}.liara.run", "envs_keys": sorted(a["envs"]),
            }

    def create_project(self, name: str, platform: str, plan_id: str, network: str | None = None) -> dict:
        self._auth("create_project")
        with _fake_state() as st:
            if name in st["apps"]:
                raise LiaraError("bad_request", 409, "این نام قبلاً در لیارا استفاده شده است.")
            st["apps"][name] = {
                "name": name, "platform": platform, "plan_id": plan_id, "status": "RUNNING",
                "created_at": _now_iso(), "envs": {}, "scenario": "healthy", "port": 80,
                "current": "", "releases": [],
            }
        return {"name": name, "platform": platform, "plan_id": plan_id}

    def get_logs(self, name: str, since: int | None = None) -> list[dict]:
        self._auth("get_logs")
        with _fake_state() as st:
            a = self._app(st, name)
            sc, port = a["scenario"], a["port"]
        if sc == "missing_package":
            msgs = [
                "Traceback (most recent call last):",
                '  File "/app/manage.py", line 10, in <module>',
                "    import requests",
                "ModuleNotFoundError: No module named 'requests'",
                "Container exited with code 1; restarting (crash loop)",
            ] * 2
        elif sc == "oom":
            msgs = ["Processing large request...", "Killed", "Container OOMKilled (memory limit reached)", "Restarting container after OOMKilled"] * 2
        elif sc == "missing_env":
            msgs = [
                "django.core.exceptions.ImproperlyConfigured: set the SECRET_KEY environment variable",
                "Container exited with code 1; restarting (crash loop)",
            ] * 2
        else:
            msgs = [f"Listening on port {port}", "GET / 200 12ms", "GET /health 200 2ms"]
        msgs = ["Starting application..."] + msgs
        rows = [{"ts": _now_iso(-(len(msgs) - i) * 5), "message": m} for i, m in enumerate(msgs)]
        if since is not None:
            cutoff = datetime.fromtimestamp(float(since), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            rows = [r for r in rows if r["ts"] >= cutoff]
        return rows[-MAX_LOG_LINES:]

    def list_releases(self, name: str) -> dict:
        self._auth("list_releases")
        with _fake_state() as st:
            a = self._app(st, name)
            return {"current": a["current"], "releases": copy.deepcopy(list(reversed(a["releases"])))}

    def get_metrics_summary(self, name: str) -> dict:
        self._auth("get_metrics_summary")
        with _fake_state() as st:
            a = self._app(st, name)
            ram_bytes = self._plan(a["plan_id"])["ram_mb"] * 1024 * 1024
            ratio = {"oom": 0.95, "missing_package": 0.0, "missing_env": 0.0}.get(a["scenario"], 0.35)
        now = int(time.time())
        cpu = 0.9 if ratio > 0.9 else (0.0 if ratio == 0 else 0.12)
        return {
            "cpuUsage": [{"applet": f"{name}-0", "value": [[now - 60 * i, str(cpu)] for i in range(3)]}],
            "memoryUsage": [{"applet": f"{name}-0", "value": [[now - 60 * i, str(int(ram_bytes * ratio))] for i in range(3)]}],
            "disksUsage": [{"name": "default", "reportedAt": _now_iso(), "size": "1073741824", "usage": "104857600"}],
        }

    def restart(self, name: str) -> None:
        self._auth("restart")
        with _fake_state() as st:
            a = self._app(st, name)
            a["restarted_at"] = time.time()
            if a["scenario"] == "oom":  # a restart alone does not fix a memory problem
                a["status"] = "CRASH_LOOP"

    def resize(self, name: str, plan_id: str) -> None:
        self._auth("resize")
        if plan_id not in {p["id"] for p in _FAKE_PLANS}:
            raise LiaraError("bad_request", 400, "پلن نامعتبر است.")
        with _fake_state() as st:
            a = self._app(st, name)
            a["plan_id"] = plan_id
            if a["scenario"] == "oom" and self._plan(plan_id)["ram_mb"] >= 1024:
                a["scenario"], a["status"] = "healthy", "RUNNING"

    def update_envs(self, name: str, variables: dict[str, str], merge: bool = True) -> None:
        self._auth("update_envs")
        with _fake_state() as st:
            a = self._app(st, name)
            if not merge:
                a["envs"] = {}
            a["envs"].update({str(k): str(v) for k, v in variables.items()})
            if a["scenario"] == "missing_env" and "SECRET_KEY" in a["envs"]:
                a["scenario"], a["status"] = "healthy", "RUNNING"

    # ---- deploy

    def upload_source(self, name: str, archive_path: str | Path) -> str:
        self._auth("upload_source")
        path = Path(archive_path)
        blob = path.name.encode()
        with contextlib.suppress(Exception):
            with tarfile.open(path, "r:*") as tar:
                for m in tar.getmembers():
                    blob += m.name.encode()
                    if m.isfile() and m.size < 1_000_000:
                        blob += tar.extractfile(m).read()  # type: ignore[union-attr]
        with _fake_state() as st:
            self._app(st, name)
            sid = "src-" + uuid.uuid4().hex[:10]
            st["sources"][sid] = {"app": name, "fail": b"FORCE_BUILD_FAIL" in blob}
        return sid

    def create_release(self, name: str, source_id: str, platform: str, port: int, message: str = "",
                       build_args: dict[str, str] | None = None, health_check: dict | None = None) -> str:
        self._auth("create_release")
        with _fake_state() as st:
            self._app(st, name)
            src = st["sources"].get(source_id)
            seeded = source_id.startswith("src-") and any(source_id == r.get("source_id") for a in st["apps"].values() for r in a["releases"])
            if src is None and not seeded:
                raise LiaraError("not_found", 404)
            rid = "rel-" + uuid.uuid4().hex[:10]
            st["builds"][rid] = {"app": name, "polls": 0, "fail": bool(src and src["fail"]), "source_id": source_id,
                                 "platform": platform, "port": port, "created_at": _now_iso()}
        return rid

    def get_build_logs(self, release_id: str, since: int | None = None) -> dict:
        """Poll 1 -> DEPLOYING, poll 2 and later -> READY (or FAILED). `since` is a line index here."""
        self._auth("get_build_logs")
        with _fake_state() as st:
            b = st["builds"].get(release_id)
            if b is None:
                raise LiaraError("not_found", 404)
            if b["polls"] < 99 and b.get("state") not in ("READY", "FAILED", "CANCELED"):
                b["polls"] += 1
            all_lines = ["Step 1/4 : FROM base image", "Step 2/4 : COPY . /app", "Step 3/4 : install dependencies"]
            reason = ""
            if b.get("state") == "CANCELED":
                state = "CANCELED"
            elif b["polls"] < 2:
                state = "DEPLOYING"
            elif b["fail"]:
                state, reason = "FAILED", "package.json has no start script"
                all_lines.append("ERROR: " + reason)
            else:
                state = "READY"
                all_lines.append("Image pushed. Release is healthy.")
            if state in ("READY", "FAILED") and b.get("state") != state:
                b["state"] = state
                app = st["apps"][b["app"]]
                app["releases"].append({"id": release_id, "state": state, "created_at": b["created_at"],
                                        "source_id": b["source_id"], "platform": b["platform"], "port": b["port"]})
                if state == "READY":
                    app["current"], app["status"], app["scenario"] = release_id, "RUNNING", "healthy"
                    app["port"] = b["port"]
            start = int(since or 0)
            return {"state": state, "fail_reason": reason, "lines": all_lines[start:], "next_since": len(all_lines)}

    def cancel_build(self, release_id: str) -> None:
        self._auth("cancel_build")
        with _fake_state() as st:
            b = st["builds"].get(release_id)
            if b is None:
                raise LiaraError("not_found", 404)
            if b.get("state") not in ("READY", "FAILED"):
                b["state"] = "CANCELED"

    def rollback(self, name: str, release_id: str) -> str:
        self._auth("rollback")
        info = self.list_releases(name)
        target = next((r for r in info["releases"] if r["id"] == release_id), None)
        if target is None:
            raise LiaraError("not_found", 404)
        if not target.get("source_id"):
            raise LiaraError("bad_request", message_fa="سورس این نسخه در دسترس نیست.")
        return self.create_release(name, target["source_id"], target["platform"], int(target.get("port") or 80), message=f"rollback to {release_id}")

    # ---- plans, databases, storage

    def list_plans(self) -> list[dict]:
        self._auth("list_plans")
        return copy.deepcopy(_FAKE_PLANS)

    def list_databases(self) -> list[dict]:
        self._auth("list_databases")
        with _fake_state() as st:
            return [{"id": d["id"], "type": d["type"], "status": d["status"], "plan_id": d["plan_id"], "host": d["host"]} for d in st["dbs"].values()]

    def list_networks(self) -> list[dict]:
        self._auth("list_networks")
        return [{"id": "fake-net", "name": "assistant"}]

    def create_network(self, name: str) -> None:
        self._auth("create_network")

    def create_database(self, type: str, name: str, plan_id: str, version: str | None = None, network: str | None = None) -> dict:
        self._auth("create_database")
        with _fake_state() as st:
            db_id = "db-" + uuid.uuid4().hex[:8]
            st["dbs"][db_id] = {"id": db_id, "type": type, "name": name, "plan_id": plan_id, "status": "RUNNING",
                                "host": f"{name}.fake-db.liara.local", "internal_host": name,
                                "port": 5432 if type == "postgresql" else 3306,
                                "username": "root", "password": uuid.uuid4().hex[:16], "db_name": "app"}
        return {"id": db_id}

    def get_database(self, db_id: str) -> dict:
        self._auth("get_database")
        with _fake_state() as st:
            d = st["dbs"].get(db_id)
            if d is None:
                raise LiaraError("not_found", 404)
            return {k: d[k] for k in ("id", "type", "status", "host", "port", "internal_host", "username", "password", "db_name")}

    def list_buckets(self) -> list[dict]:
        self._auth("list_buckets")
        with _fake_state() as st:
            return list(st["buckets"].values())

    def create_bucket(self, name: str, plan: str, permission: str = "private") -> dict:
        self._auth("create_bucket")
        if plan not in _FAKE_BUCKET_PLANS:
            raise LiaraError("bad_request", 400, "پلن فضای ذخیره‌سازی نامعتبر است.")
        with _fake_state() as st:
            if name in st["buckets"]:
                raise LiaraError("bad_request", 409, "این نام قبلاً استفاده شده است.")
            st["buckets"][name] = {"id": "bk-" + uuid.uuid4().hex[:8], "name": name, "plan": plan, "permission": permission, "status": "active"}
        return {"name": name, "plan": plan, "permission": permission}

    def create_bucket_key(self, name: str) -> dict:
        self._auth("create_bucket_key")
        with _fake_state() as st:
            if name not in st["buckets"]:
                raise LiaraError("not_found", 404)
            key = {"access_key": uuid.uuid4().hex[:20].upper(), "secret_key": uuid.uuid4().hex}
            st["keys"].append({"bucket": name, "access_key": key["access_key"]})
        return key


# --------------------------------------------------------------------------- factories

def client_for_token(token: str, team_id: str = "") -> LiaraClient | FakeLiaraClient:
    """Client for a raw token, used by the connect flow before a connection exists."""
    if settings.LIARA_FAKE:
        return FakeLiaraClient(token, team_id)
    return LiaraClient(token, team_id)


def make_client(connection: Any) -> LiaraClient | FakeLiaraClient:
    """Client for a LiaraConnection (decrypts its token)."""
    return client_for_token(connection.get_token(), connection.team_id)


__all__ = [
    "LiaraError", "LiaraClient", "FakeLiaraClient", "make_client", "client_for_token", "check_url",
    "build_archive", "fake_set_scenario", "fake_check_url", "fake_reset", "redact_obj",
]
