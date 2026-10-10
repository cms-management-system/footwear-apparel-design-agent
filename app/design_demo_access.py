"""Reversible local demo personnel sessions; business authority stays managed."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import time
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select

from .config import get_config
from .models import DesignDemoChain, DesignDemoSession, DesignerSession, DesignerUser, session

ENTRIES = {"designer": "/demo/designer", "manager": "/demo/manager"}
TTL = 12 * 3600


def error(code, message, status=409):
    from .agent.store import AgentError
    return AgentError(code, message, status)


def _secret():
    path = get_config().design_demo_secret_file
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as target:
            target.write(secrets.token_bytes(48))
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise error("DEMO_CONFIGURATION_INVALID", "演示本地秘密文件必须为私有普通文件", 503)
    value = path.read_bytes()
    if len(value) != 48:
        raise error("DEMO_CONFIGURATION_INVALID", "演示恢复秘密损坏，请由运行负责人核实", 503)
    return value


def _subject(role):
    cfg = get_config()
    return cfg.design_demo_designer_username if role == "designer" else cfg.design_manager_username


def _mapping(db, role):
    user = db.get(DesignerUser, _subject(role), populate_existing=True)
    if not user or not user.active or user.role != role:
        raise error("DEMO_CONFIGURATION_INVALID", "演示人员映射必须指向已启用的对应角色", 503)
    return user


def initialize():
    """Only an empty personnel database may acquire synthetic accounts once."""
    cfg = get_config()
    if not cfg.design_demo_access_enabled:
        return
    _secret()
    if not cfg.design_manager_username or cfg.design_manager_username == cfg.design_demo_designer_username:
        raise error("DEMO_CONFIGURATION_INVALID", "请配置不同的演示员工与经理稳定主体", 503)
    with session() as db:
        if not db.scalar(select(DesignerUser.username).limit(1)):
            from .design_auth import password_hash
            for role in ENTRIES:
                db.add(DesignerUser(
                    username=_subject(role), role=role, active=True,
                    display_name="演示设计员工" if role == "designer" else "演示设计经理",
                    password_hash=password_hash(secrets.token_urlsafe(48)),
                ))
            db.commit()
        for role in ENTRIES:
            _mapping(db, role)


def _chain_valid(chain):
    cfg = get_config()
    return bool(chain and chain.scope_id == cfg.design_scope_id and chain.instance_id == cfg.design_instance_id
                and chain.purpose in {"interactive", "validation"})


def _recovery_raw(secret, chain):
    seed = ("validation:" + chain.payload["source_context"] if chain.purpose == "validation"
            else "interactive:" + chain.payload.get("recovery_seed", ""))
    raw = hmac.new(secret, seed.encode(), "sha256").hexdigest()
    from .design_auth import token_hash
    if token_hash(raw) != chain.id:
        raise error("DEMO_RECOVERY_BLOCKED", "演示恢复链损坏，禁止建立新的预算身份")
    return raw


def session_enabled(db, record):
    """Used by HTTP, transaction, streaming and workers, including after disable."""
    if not record or record.expires_at <= time.time():
        return False
    marker = db.get(DesignDemoSession, record.token_hash, populate_existing=True)
    if not marker:
        return True
    cfg = get_config()
    chain = db.get(DesignDemoChain, marker.chain_id, populate_existing=True)
    if not (cfg.design_demo_access_enabled and _chain_valid(chain) and chain.active
            and not chain.payload.get("validation_successor")
            and chain.selected_role == marker.role and marker.subject == record.username
            and marker.subject == _subject(marker.role)):
        return False
    user = db.get(DesignerUser, record.username, populate_existing=True)
    if not user or not user.active or user.role != marker.role:
        return False
    from .agent.managed_store import ExecutionContextPolicy
    from .design_auth import session_context
    ctx = db.get(ExecutionContextPolicy, session_context(record.token_hash), populate_existing=True)
    if chain.purpose == "validation":
        origin = db.get(ExecutionContextPolicy, chain.payload.get("source_context"), populate_existing=True)
        if not origin or origin.purpose != "validation" or origin.payload != chain.payload.get("validation"):
            return False
    return bool(ctx and ctx.subject == record.username and ctx.purpose == chain.purpose
                and (chain.purpose != "validation" or ctx.payload == chain.payload.get("validation")))


def request_token(request):
    cfg = get_config()
    # Once a browser has selected demo access, a stale demo cookie never falls
    # back to its untouched ordinary cookie, even when demo mode is disabled.
    demo = getattr(request.state, "design_demo_token", None) or request.cookies.get(cfg.design_demo_cookie)
    if cfg.design_demo_access_enabled or demo:
        return demo or ""
    return request.cookies.get(cfg.design_session_cookie, "")


def session_for_token_hash(db, digest, *, demo=False):
    """One transaction-local personnel session resolver, also for hashed access metadata."""
    if not digest:
        return None
    record = db.get(DesignerSession, digest, populate_existing=True)
    if demo and not db.get(DesignDemoSession, digest, populate_existing=True):
        return None
    return record if session_enabled(db, record) else None


def session_for_request(db, request):
    from .design_auth import token_hash
    token = request_token(request)
    return session_for_token_hash(db, token_hash(token) if token else "", demo=is_demo_request(request))


def _local_request(request):
    cfg = get_config()
    if cfg.public_demo:
        access_origin_guard(request)
        return
    host = request.url.hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"} and not host.endswith(".localhost"):
        raise error("DEMO_LOCAL_ONLY", "演示访问仅允许本机入口", 403)


def access_origin_guard(request):
    """Exact public authority or fixed headers from the actual loopback proxy peer."""
    cfg = get_config()
    origin = request.headers.get("origin")
    if origin is not None and origin != cfg.design_public_origin:
        raise error("ORIGIN_FORBIDDEN", "请求来源未获允许", 403)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin != cfg.design_public_origin:
        raise error("ORIGIN_FORBIDDEN", "公网写入必须来自当前HTTPS演示入口", 403)
    public = urlsplit(cfg.design_public_origin)
    direct = request.url.scheme == "https" and request.url.netloc == public.netloc
    forwarded = bool(
        request.client and request.client.host in {"127.0.0.1", "::1"}
        and request.headers.get("x-forwarded-host") == public.netloc
        and request.headers.get("x-forwarded-proto") == "https"
    )
    if not (direct or forwarded):
        raise error("DEMO_PUBLIC_ORIGIN_REQUIRED", "请从已配置的HTTPS演示入口访问", 403)


def bootstrap(request: Request, role=None):
    cfg = get_config()
    if not cfg.design_demo_access_enabled:
        raise error("DEMO_ACCESS_DISABLED", "演示访问已关闭", 404)
    _local_request(request)
    from .agent.managed_store import ExecutionContextPolicy
    from .design_auth import session_context, token_hash

    secret = _secret()
    fingerprint = hashlib.sha256(secret).hexdigest()
    raw_chain = request.cookies.get(cfg.design_demo_chain_cookie)
    with session() as db:
        if db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        chain = db.get(DesignDemoChain, token_hash(raw_chain)) if raw_chain else None
        if not raw_chain and request.cookies.get(cfg.design_demo_cookie):
            digest = token_hash(request.cookies[cfg.design_demo_cookie])
            marker = db.get(DesignDemoSession, digest)
            chain = db.get(DesignDemoChain, marker.chain_id) if marker else None
            if not _chain_valid(chain):
                raise error("DEMO_RECOVERY_BLOCKED", "演示会话关联无法恢复，禁止回退创作额度")
            raw_chain = _recovery_raw(secret, chain)
        if raw_chain and (not _chain_valid(chain) or chain.payload.get("secret_sha256") != fingerprint):
            raise error("DEMO_RECOVERY_BLOCKED", "演示用途关联无法恢复，请由运行负责人核实")
        if chain and chain.payload.get("validation_successor"):
            chain = db.get(DesignDemoChain, chain.payload["validation_successor"])
            if not _chain_valid(chain) or chain.purpose != "validation":
                raise error("DEMO_RECOVERY_BLOCKED", "验收恢复关联损坏，禁止回退创作额度")
            seed = "validation:" + chain.payload["source_context"]
            raw_chain = hmac.new(secret, seed.encode(), "sha256").hexdigest()
        # Read the ordinary cookie classification even if its session expired.
        # It is never overwritten and remains the authoritative validation link.
        ordinary = request.cookies.get(cfg.design_session_cookie)
        source = db.get(DesignerSession, token_hash(ordinary)) if ordinary else None
        source_context = session_context(token_hash(ordinary)) if ordinary else None
        classification = db.get(ExecutionContextPolicy, source_context) if source_context else None
        validation = classification if classification and classification.purpose == "validation" else None
        if validation and not source:
            raise error("DEMO_RECOVERY_BLOCKED", "原验收会话关联缺失，禁止回退创作额度")
        previous_chain = chain if validation and chain and chain.purpose != "validation" else None
        if validation and (not chain or chain.purpose != "validation"):
            raw_chain = hmac.new(secret, ("validation:" + source_context).encode(), "sha256").hexdigest()
            chain = db.get(DesignDemoChain, token_hash(raw_chain))
        if chain and chain.purpose == "validation":
            origin = db.get(ExecutionContextPolicy, chain.payload.get("source_context"))
            if (not origin or origin.purpose != "validation" or origin.payload != chain.payload.get("validation")
                    or (validation and validation.payload != origin.payload)):
                raise error("DEMO_RECOVERY_BLOCKED", "验收用途关联无法恢复，禁止回退创作额度")
        if not chain:
            recovery_seed = secrets.token_hex(32)
            raw_chain = raw_chain or hmac.new(secret, ("interactive:" + recovery_seed).encode(), "sha256").hexdigest()
            chain = DesignDemoChain(
                id=token_hash(raw_chain), scope_id=cfg.design_scope_id, instance_id=cfg.design_instance_id,
                purpose="validation" if validation else "interactive", selected_role=role or "designer", active=True,
                payload={"secret_sha256": fingerprint, "recovery_seed": recovery_seed,
                         **({"source_context": source_context,
                         "validation": validation.payload} if validation else {})},
            )
            db.add(chain)
            db.flush()
        if previous_chain:
            previous_chain.active = False
            previous_chain.payload = {**previous_chain.payload, "validation_successor": chain.id}
        if chain.payload.get("secret_sha256") != fingerprint:
            raise error("DEMO_RECOVERY_BLOCKED", "演示恢复秘密已变化，请由运行负责人核实")
        selected = role or chain.selected_role
        user = _mapping(db, selected)
        chain.selected_role, chain.active = selected, True
        raw = hmac.new(secret, (chain.id + ":" + selected + ":" + user.username).encode(), "sha256").hexdigest()
        digest = token_hash(raw)
        record = db.get(DesignerSession, digest)
        if record and record.username != user.username:
            raise error("DEMO_RECOVERY_BLOCKED", "演示主体映射已变化")
        marker = db.get(DesignDemoSession, digest) if record else None
        if record and (not marker or marker.chain_id != chain.id or marker.role != selected
                       or marker.subject != user.username):
            raise error("DEMO_RECOVERY_BLOCKED", "演示会话标记缺失或已变化")
        if not record:
            record = DesignerSession(token_hash=digest, username=user.username, expires_at=int(time.time()) + TTL)
            db.add(record)
            db.add(DesignDemoSession(token_hash=digest, chain_id=chain.id, role=selected, subject=user.username))
        else:
            record.expires_at = int(time.time()) + TTL
        context = session_context(digest)
        payload = chain.payload.get("validation", {})
        prior = db.get(ExecutionContextPolicy, context)
        if prior and (prior.subject != user.username or prior.purpose != chain.purpose or prior.payload != payload):
            raise error("DEMO_RECOVERY_BLOCKED", "演示执行用途已冻结，禁止覆盖")
        if not prior:
            db.add(ExecutionContextPolicy(auth_context_id=context, subject=user.username,
                                         purpose=chain.purpose, payload=payload))
        db.commit()
    request.state.design_demo_token = raw
    request.state.design_demo_cookies = {cfg.design_demo_cookie: raw, cfg.design_demo_chain_cookie: raw_chain}


def apply_cookies(request, response):
    cfg = get_config()
    for name, value in getattr(request.state, "design_demo_cookies", {}).items():
        response.set_cookie(name, value, httponly=True, samesite="strict", path=cfg.design_cookie_path,
                            secure=cfg.design_session_cookie_secure,
                            max_age=365 * 24 * 3600 if name.endswith("_chain") else TTL)


def inherit_validation(db, request, context, subject):
    """Explicit normal login ends demo access, but never renews validation quota."""
    from .agent.managed_store import ExecutionContextPolicy
    from .design_auth import session_context, token_hash
    cfg = get_config()
    ordinary = request.cookies.get(cfg.design_session_cookie)
    prior = db.get(ExecutionContextPolicy, session_context(token_hash(ordinary))) if ordinary else None
    chain_cookie = request.cookies.get(cfg.design_demo_chain_cookie)
    chain = db.get(DesignDemoChain, token_hash(chain_cookie)) if chain_cookie else None
    if not chain and request.cookies.get(cfg.design_demo_cookie):
        marker = db.get(DesignDemoSession, token_hash(request.cookies[cfg.design_demo_cookie]))
        chain = db.get(DesignDemoChain, marker.chain_id) if marker else None
    if chain and chain.payload.get("validation_successor"):
        chain = db.get(DesignDemoChain, chain.payload["validation_successor"])
    if chain and chain.purpose == "validation":
        source = db.get(ExecutionContextPolicy, chain.payload.get("source_context"))
        if not source or source.purpose != "validation" or source.payload != chain.payload.get("validation"):
            raise error("DEMO_RECOVERY_BLOCKED", "原验收恢复关联损坏，禁止回退创作额度")
        prior = source
    if prior and prior.purpose == "validation":
        db.add(ExecutionContextPolicy(auth_context_id=context, subject=subject,
                                     purpose="validation", payload=prior.payload))


def is_demo_request(request):
    return bool(getattr(request.state, "design_demo_token", None)
                or request.cookies.get(get_config().design_demo_cookie))


def logout(request, response):
    from .agent import execution_quota
    from .agent.store import Record
    from .design_auth import session_context, token_hash
    cfg = get_config()
    raw = request.cookies.get(cfg.design_demo_cookie)
    if raw:
        with session() as db:
            if db.bind.dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            marker = db.get(DesignDemoSession, token_hash(raw))
            if marker:
                chain = db.get(DesignDemoChain, marker.chain_id)
                chain.active = False
                record = db.get(DesignerSession, marker.token_hash)
                if record:
                    record.expires_at = 0
                context = session_context(marker.token_hash)
                for run in db.scalars(select(Record).where(Record.kind == "creation_run")):
                    if run.payload.get("auth_context_id") == context and run.status in {"queued", "running"}:
                        run.payload = {**run.payload, "demo_access_revoked": True}
                        execution_quota.release_unsent(db, run)
                for task in db.scalars(select(Record).where(Record.kind == "task")):
                    if task.status in {"queued", "awaiting_input"}:
                        run_id = task.payload.get("creation_run_id")
                        run = db.get(Record, run_id) if run_id else None
                        if (task.payload.get("auth_context_id") == context
                                or (run and run.payload.get("auth_context_id") == context)):
                            task.status = "cancelled"
                            if run and run.status in {"queued", "running"}:
                                run.status = "blocked"
                                run.payload = {**run.payload, "blocked_reason": "DEMO_SESSION_ENDED"}
                db.commit()
    response.delete_cookie(cfg.design_demo_cookie, path=cfg.design_cookie_path,
                           secure=cfg.design_session_cookie_secure, httponly=True, samesite="strict")
    return {"ok": True, "access_mode": "demo", "demo_entry_urls": ENTRIES}
