"""站点列表与代理地址的规范化工具。

这两个配置都来自用户手输，格式五花八门（``127.0.0.1:7897``、``https://a.com/``、
``socks5://u:p@host:1080``），统一在这里收敛成可直接交给 httpx / yt-dlp 的形式。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit
import importlib.util
import json
import re

from .models import SiteConfig
from .profiles import DEFAULT_PROFILE, detect_profile, normalize_profile, profile_label, rules_for

PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}
SOCKS_SCHEMES = {"socks5", "socks5h", "socks4", "socks4a"}
SITE_SCHEMES = {"http", "https"}

# 站点名/ID 里允许出现的字符，同时用于兜底生成 ID
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def host_of(url: str) -> str:
    """取出 URL 的 netloc（含端口），解析失败返回空串。"""
    try:
        return urlsplit(url.strip()).netloc.lower()
    except ValueError:
        return ""


def normalize_base_url(value: str) -> str:
    """把用户输入的站点整理成 ``scheme://host[:port]``。

    站点内部所有路径都是绝对路径（``/v.php``），所以这里刻意丢掉 path，
    避免出现 ``https://a.com/91/v.php`` 这种拼接错误。
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("站点地址不能为空")
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:  # 端口非法等情况
        raise ValueError("站点地址格式不正确，应为 域名 或 https://域名") from exc
    scheme = parts.scheme.lower()
    if scheme not in SITE_SCHEMES:
        raise ValueError("站点地址只支持 http/https，例如 91porn.com 或 https://a.example.com")
    host = parts.hostname or ""
    if not host or any(char in host for char in " /\\"):
        raise ValueError("站点地址格式不正确，应为 域名 或 https://域名")
    netloc = host if port is None else f"{host}:{port}"
    if parts.username or parts.password:
        raise ValueError("站点地址不要带用户名密码")
    return f"{scheme}://{netloc}"


def normalize_proxy(value: str) -> str:
    """把 ``127.0.0.1:7897`` / ``http://u:p@h:1`` 整理成标准代理 URL。"""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ValueError("代理地址格式不正确，例如 127.0.0.1:7897") from exc
    scheme = parts.scheme.lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError("代理协议只支持 http/https/socks5/socks4，例如 127.0.0.1:7897")
    if not parts.hostname:
        raise ValueError("代理地址缺少主机名，例如 127.0.0.1:7897")
    if port is None:
        raise ValueError("代理地址缺少端口，例如 127.0.0.1:7897")
    auth = ""
    if parts.username:
        auth = parts.username
        if parts.password:
            auth = f"{auth}:{parts.password}"
        auth = f"{auth}@"
    return f"{scheme}://{auth}{parts.hostname}:{port}"


def socks_available() -> bool:
    """httpx 的 SOCKS 支持依赖 socksio，缺了会直接抛 ImportError。"""
    return importlib.util.find_spec("socksio") is not None


def proxy_needs_socks(proxy: str) -> bool:
    try:
        return urlsplit(proxy.strip()).scheme.lower() in SOCKS_SCHEMES
    except ValueError:
        return False


def site_id_from_url(base_url: str, taken: set[str] | None = None) -> str:
    """用域名生成稳定的站点 ID，冲突时补序号。"""
    host = urlsplit(base_url).hostname or "site"
    slug = _SLUG_RE.sub("-", host.lower()).strip("-") or "site"
    candidate = slug
    taken = taken or set()
    index = 2
    while candidate in taken:
        candidate = f"{slug}-{index}"
        index += 1
    return candidate


def site_name_from_url(base_url: str) -> str:
    return urlsplit(base_url).hostname or base_url


@dataclass
class SiteEntry:
    """站点列表中的一项：``id`` 由域名派生，用于稳定引用。

    除了域名，每个站点还自带一整套解析规则（``rules``）。以前切站点只改
    ``site.json`` 的 ``base_url``，规则共用一份，碰上页面结构完全不同的镜像站
    就会一条都抓不到；现在切站点等于把该站点的规则整份写进 ``site.json``。
    """

    id: str
    name: str
    base_url: str
    profile: str = DEFAULT_PROFILE
    rules: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "profile": self.profile,
            "profile_label": profile_label(self.profile),
        }


class SiteBook:
    """维护「站点列表 + 当前生效站点」，并同步写回 site.json。

    site.json 依旧是唯一的事实来源（CLI、采集器、下载器都读它）。
    切站点时不仅改写 base_url，还把这套站点自己的解析规则一并写进去，
    这样不同结构的镜像站可以共存。
    """

    def __init__(self, path: Path, config_path: Path) -> None:
        self.path = path
        self.config_path = config_path
        self.lock = Lock()
        self.entries: list[SiteEntry] = []
        self.active_id = ""
        self._load()

    def _current_base_url(self) -> str:
        try:
            return SiteConfig.load(self.config_path).base_url
        except (OSError, ValueError, KeyError):
            return ""

    def _current_rules(self) -> dict:
        """读取 site.json 里除 base_url 之外的全部规则。"""
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        raw.pop("base_url", None)
        return raw

    def _load(self) -> None:
        current = self._current_base_url()
        current_rules = self._current_rules() if current else {}
        raw: dict = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
        entries: list[SiteEntry] = []
        seen: set[str] = set()
        for item in raw.get("sites", []) if isinstance(raw, dict) else []:
            try:
                base_url = normalize_base_url(str(item.get("base_url", "")))
            except ValueError:
                continue
            identifier = str(item.get("id") or "").strip() or site_id_from_url(base_url, seen)
            if identifier in seen:
                identifier = site_id_from_url(base_url, seen)
            seen.add(identifier)
            profile = normalize_profile(str(item.get("profile") or "") or detect_profile(base_url))
            rules = item.get("rules")
            entries.append(
                SiteEntry(
                    id=identifier,
                    name=str(item.get("name") or site_name_from_url(base_url))[:60],
                    base_url=base_url,
                    profile=profile,
                    rules=rules if isinstance(rules, dict) else {},
                )
            )
        # site.json 里的地址如果不在列表里（首次运行 / 手工改过），作为默认项补进去
        if current:
            try:
                normalized = normalize_base_url(current)
            except ValueError:
                normalized = ""
            if normalized and all(host_of(entry.base_url) != host_of(normalized) for entry in entries):
                entries.insert(
                    0,
                    SiteEntry(
                        id=site_id_from_url(normalized, seen),
                        name=site_name_from_url(normalized),
                        base_url=normalized,
                        profile=normalize_profile(str(current_rules.get("profile") or "") or detect_profile(normalized)),
                        rules={},
                    ),
                )
        # 规则补齐：老版本 sites.json 没有 rules 字段。
        # 当前生效站点直接采用 site.json 里的规则（手工调过的选择器不会被覆盖），
        # 其余站点用自己模板的预设。
        for entry in entries:
            if current and host_of(entry.base_url) == host_of(current) and current_rules:
                entry.rules = dict(current_rules)
                entry.profile = normalize_profile(str(current_rules.get("profile") or "") or entry.profile)
            elif not entry.rules:
                entry.rules = rules_for(entry.profile)
            # 规则里始终写明模板名：切到该站点时写回的 site.json 才自带 profile，
            # 不会因为缺这个键而回落到默认值
            entry.rules["profile"] = entry.profile
        self.entries = entries
        wanted = str(raw.get("active_id", "")) if isinstance(raw, dict) else ""
        match = next((entry for entry in entries if entry.id == wanted), None)
        if match is None and current:
            match = next((entry for entry in entries if host_of(entry.base_url) == host_of(current)), None)
        if match is None and entries:
            match = entries[0]
        self.active_id = match.id if match else ""

    def save(self) -> None:
        with self.lock:
            content = json.dumps(
                {
                    "active_id": self.active_id,
                    "sites": [
                        {
                            "id": entry.id,
                            "name": entry.name,
                            "base_url": entry.base_url,
                            "profile": entry.profile,
                            "rules": entry.rules,
                        }
                        for entry in self.entries
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self) -> list[SiteEntry]:
        with self.lock:
            return [
                SiteEntry(entry.id, entry.name, entry.base_url, entry.profile, dict(entry.rules))
                for entry in self.entries
            ]

    def active(self) -> SiteEntry | None:
        with self.lock:
            return next((entry for entry in self.entries if entry.id == self.active_id), None)

    def add(self, name: str, base_url: str, profile: str = "") -> SiteEntry:
        normalized = normalize_base_url(base_url)
        chosen = normalize_profile(profile) if (profile or "").strip() else detect_profile(normalized)
        with self.lock:
            existing = next((entry for entry in self.entries if host_of(entry.base_url) == host_of(normalized)), None)
            if existing is not None:
                return existing
            taken = {entry.id for entry in self.entries}
            entry = SiteEntry(
                id=site_id_from_url(normalized, taken),
                name=(name or "").strip()[:60] or site_name_from_url(normalized),
                base_url=normalized,
                profile=chosen,
                rules=rules_for(chosen),
            )
            self.entries.append(entry)
        self.save()
        return entry

    def remove(self, site_id: str) -> bool:
        with self.lock:
            target = next((entry for entry in self.entries if entry.id == site_id), None)
            if target is None:
                return False
            if len(self.entries) == 1:
                raise ValueError("至少保留一个站点，无法删除")
            self.entries = [entry for entry in self.entries if entry.id != site_id]
            if self.active_id == site_id:
                self.active_id = self.entries[0].id
                switch = self.entries[0].base_url
                rules = dict(self.entries[0].rules)
            else:
                switch = ""
                rules = {}
        self.save()
        if switch:
            self.apply(switch, rules)
        return True

    def apply(self, base_url: str, rules: dict | None = None) -> None:
        """把 site.json 换成新站点的地址和规则。

        没有传 rules 时只改 base_url（保持老行为）。传了 rules 就以 rules 为准：
        上一套规则里独有的键（比如 pinse 的 playback_url）必须被清掉，否则切回
        91porn 后会留下半套 pinse 配置；用户在 site.json 里自己加的旁路键保留。
        """
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取站点配置文件 site.json：{exc}") from exc
        known = {item.name for item in fields(SiteConfig)}
        merged = {
            key: value
            for key, value in (raw.items() if isinstance(raw, dict) else [])
            if key not in known
        }
        if rules:
            merged.update({key: value for key, value in rules.items() if key in known})
        merged["base_url"] = base_url
        content = json.dumps(merged, ensure_ascii=False, indent=2)
        temporary = self.config_path.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(self.config_path)

    def set_active(self, site_id: str) -> SiteEntry:
        with self.lock:
            target = next((entry for entry in self.entries if entry.id == site_id), None)
            if target is None:
                raise ValueError("站点不存在")
            self.active_id = target.id
            url = target.base_url
            rules = dict(target.rules)
        self.save()
        self.apply(url, rules)
        return target


def rebind_cookie_domain(path: Path, new_base_url: str) -> bool:
    """切站点后把 Cookie 文件的 domain 指到新域名，否则 yt-dlp 不会发送。"""
    if not path.exists():
        return False
    host = host_of(new_base_url)
    if not host:
        return False
    domain = host if host.startswith(".") else f".{host}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines or not lines[0].lstrip().startswith("# Netscape"):
        return False
    changed = False
    output = [lines[0]]
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            output.append(line)
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            output.append(line)
            continue
        if fields[0].lower() != domain.lower():
            fields[0] = domain
            changed = True
        output.append("\t".join(fields))
    if changed:
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
        temporary.replace(path)
    return changed