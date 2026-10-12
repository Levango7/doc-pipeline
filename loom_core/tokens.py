"""Token 身份：多凭证 → Principal（任务/成本/产物作用域隔离的根）。

背景（spec §5.4「身份与租户」）：此前全站共享一个 `ADMIN_API_KEY`，
所有调用者是同一个"管理员"，任务清单彼此全见。本模块把凭证升级为
**身份**：每个 token 解析出一个 Principal，任务与成本按 Principal 隔离。

配置（`tokens.yaml`，路径可被 `LOOM_TOKENS_FILE` 覆盖；与 webhooks.yaml
同族惯例）：

    tokens:
      - id: alice                 # Principal 标识（进任务 owner 与审计）
        tenant: acme              # 可选：展示/组织用
        token_env: LOOM_TOKEN_ALICE   # 密钥经环境变量注入——不落文件
        admin: false              # true = 全可见（跨租户运维位）
      - id: ci
        token_sha256: "<64 位 hex>"   # 或直接存 SHA-256（同样不落明文）

设计决定：
  - **密钥不进文件**：只存 env 名或 SHA-256（webhooks 的 secret_env 惯例）；
  - **兼容**：`ADMIN_API_KEY` 继续有效，解析为 admin Principal（id="admin"）——
    旧部署零改动；未配置 tokens.yaml 时行为与既往完全一致（本机信任/单 key）；
  - **tokens.yaml 一旦存在，回环信任模式关闭**（必须带凭证）——启用身份
    却对回环免检等于没启用；这是"安全默认"，需要旧行为就别放 tokens.yaml；
  - **缺密钥的条目不可用且大声告警**（env 未设置 → 该 token 永远 401，
    启动日志点名）——空密钥静默启用是致命洞，不做；
  - 比较一律 `hmac.compare_digest`（常数时间）。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: tokens.yaml 的环境变量覆盖（与 webhooks.yaml 同族）
TOKENS_FILE_ENV = "LOOM_TOKENS_FILE"
DEFAULT_FILE_NAME = "tokens.yaml"


@dataclass(frozen=True)
class Principal:
    """一个凭证背后的身份。admin=True 跨租户全可见（运维位）。"""

    id: str
    tenant: str = ""
    admin: bool = False
    source: str = ""  # "admin_key" / "tokens.yaml" / "loopback"（审计可读）

    @property
    def label(self) -> str:
        return f"{self.tenant}/{self.id}" if self.tenant else self.id


#: 本机信任模式的身份（未配置任何凭证时的既有行为，保持全可见）
LOCAL_PRINCIPAL = Principal(id="local", admin=True, source="loopback")


class TokenConfigError(ValueError):
    """tokens.yaml 结构性错误（启动期硬失败，不带病运行）。"""


class _TokenEntry:
    __slots__ = ("principal", "env_name", "sha256", "usable", "why")

    def __init__(self, principal: Principal, env_name: str = "", sha256: str = ""):
        self.principal = principal
        self.env_name = env_name
        self.sha256 = sha256
        self.usable = True
        self.why = ""


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_tokens_path() -> Path:
    """tokens.yaml 解析：env 覆盖 > 仓库根（与 config.json 同处）。"""
    env = (os.environ.get(TOKENS_FILE_ENV) or "").strip()
    if env:
        return Path(env)
    return Path(__file__).parent.parent / DEFAULT_FILE_NAME


class TokenStore:
    """已解析的凭证集合：`resolve(auth_header, path, api_key)` → Principal | None。"""

    def __init__(self, entries: list[_TokenEntry], source: str = ""):
        self.entries = entries
        self.source = source

    @property
    def enabled(self) -> bool:
        """是否处于"多身份"模式（tokens.yaml 有效装载）。"""
        return bool(self.entries)

    def resolve(self, token: str) -> Principal | None:
        """凭证 → Principal；不匹配返回 None（调用方给 401）。"""
        if not token:
            return None
        for entry in self.entries:
            if not entry.usable:
                continue
            candidate = os.environ.get(entry.env_name, "") if entry.env_name else ""
            if entry.sha256:
                if hmac.compare_digest(_sha256_hex(token), entry.sha256):
                    return entry.principal
            elif candidate and hmac.compare_digest(token, candidate):
                return entry.principal
        return None

    @classmethod
    def load(cls, path: Path | None = None) -> TokenStore:
        """装载 tokens.yaml。文件不存在 → 空 Store（=旧世界，行为不变）。

        结构性错误（非映射 / tokens 非列表 / 条目缺 id 或双密钥源）→ 抛
        TokenConfigError，启动期炸掉——身份配置写错还带病运行，等于隔离静默失效。
        """
        import yaml

        p = path or default_tokens_path()
        if not p.is_file():
            return cls([], source="")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise TokenConfigError(f"tokens 配置解析失败 {p}: {e}") from e
        if not isinstance(raw, dict):
            raise TokenConfigError(f"tokens 配置必须是映射（{p}）")
        items = raw.get("tokens")
        if not isinstance(items, list) or not items:
            raise TokenConfigError(f"tokens 配置缺非空 tokens 列表（{p}）")

        entries: list[_TokenEntry] = []
        seen: set[str] = set()
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise TokenConfigError(f"tokens[{i}] 必须是映射")
            pid = str(item.get("id", "") or "").strip()
            if not pid:
                raise TokenConfigError(f"tokens[{i}] 缺 id")
            if pid in seen:
                raise TokenConfigError(f"tokens 里 id 重复: {pid}")
            seen.add(pid)
            # 注意：不能写 `item.get(k, "") or ""`——YAML 把 `token_sha256: 0…0`
            # 解析成 int 0（falsy），or "" 会把它吞成"未提供"，配置错误被静默。
            raw_env = item.get("token_env")
            raw_sha = item.get("token_sha256")
            env_name = str(raw_env).strip() if raw_env is not None else ""
            sha256 = str(raw_sha).strip().lower() if raw_sha is not None else ""
            if bool(env_name) == bool(sha256):
                raise TokenConfigError(
                    f"tokens[{i}]（{pid}）必须且只能给一个密钥源：token_env 或 token_sha256")
            if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
                raise TokenConfigError(f"tokens[{i}]（{pid}）token_sha256 不是 64 位 hex")
            entry = _TokenEntry(
                principal=Principal(
                    id=pid,
                    tenant=str(item.get("tenant", "") or "").strip(),
                    admin=bool(item.get("admin", False)),
                    source="tokens.yaml",
                ),
                env_name=env_name, sha256=sha256,
            )
            if env_name and not os.environ.get(env_name):
                # 缺密钥 ≠ 拒绝启动（可能是 CI 按需注入）；但该 token 不可用，
                # 且必须点名告警——空密钥静默通过是致命洞。
                entry.usable = False
                entry.why = f"环境变量 {env_name} 未设置"
                logger.warning(
                    "token %r 不可用：%s（该凭证一律 401，直到密钥注入）", pid, entry.why)
            entries.append(entry)
        return cls(entries, source=str(p))


def parse_bearer(auth_header: str) -> str:
    """从 Authorization 头取 Bearer 凭证（无则空串）。"""
    auth = auth_header or ""
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def token_from_query(path: str) -> str:
    """兼容 ?token=xxx（与既有 _check_auth 行为一致）。"""
    if "?" not in (path or ""):
        return ""
    for kv in path.split("?", 1)[1].split("&"):
        if kv.startswith("token="):
            return kv[6:]
    return ""
