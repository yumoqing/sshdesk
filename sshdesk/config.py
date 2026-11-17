"""会话配置：主机档案 + 本地 JSON 存储（密码默认不落盘）。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


def config_dir() -> str:
    override = os.environ.get("SSHDESK_CONFIG_DIR")
    if override:
        os.makedirs(override, exist_ok=True)
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    d = os.path.join(base, "sshdesk")
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "hosts.json")


def known_hosts_path() -> str:
    return os.path.join(config_dir(), "known_hosts")


@dataclass
class HostProfile:
    name: str = ""
    host: str = ""
    port: int = 22
    user: str = ""
    auth: str = "key"                      # key | password
    key_path: str = ""
    key_passphrase: str = ""
    password: str = ""                     # 只有勾了"记住密码"才会写盘
    remember_password: bool = False
    # 跳板机（可选）：用它建立 direct-tcpip 通道再连目标
    jump_host: str = ""
    jump_port: int = 22
    jump_user: str = ""
    jump_auth: str = "key"
    jump_key_path: str = ""
    jump_password: str = ""
    # 终端
    font_size: int = 11
    font_family: str = "Consolas"
    # 主机密钥策略：auto=首连信任并记录(TOFU)；strict=必须与 known_hosts 一致
    hostkey: str = "auto"
    auto_add_key: bool = True

    def display(self) -> str:
        j = " → %s@%s" % (self.jump_user, self.jump_host) if self.jump_host else ""
        return "%s%s" % (self.name or "%s@%s" % (self.user, self.host), j)

    def to_dict(self) -> dict:
        d = asdict(self)
        if not self.remember_password:
            d["password"] = ""
            d["jump_password"] = ""
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HostProfile":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class AppConfig:
    hosts: List[HostProfile] = field(default_factory=list)
    font_size: int = 11
    font_family: str = "Consolas"
    show_file_panel: bool = True

    # ---- 读写 ----
    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppConfig":
        p = path or config_path()
        if not os.path.isfile(p):
            return cls()
        try:
            with open(p, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return cls()
        return cls(hosts=[HostProfile.from_dict(h) for h in raw.get("hosts", [])],
                   font_size=raw.get("font_size", 11),
                   font_family=raw.get("font_family", "Consolas"),
                   show_file_panel=raw.get("show_file_panel", True))

    def save(self, path: Optional[str] = None) -> str:
        p = path or config_path()
        data = {"hosts": [h.to_dict() for h in self.hosts],
                "font_size": self.font_size,
                "font_family": self.font_family,
                "show_file_panel": self.show_file_panel}
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return p

    def upsert(self, h: HostProfile) -> None:
        for i, old in enumerate(self.hosts):
            if old.name == h.name and h.name:
                self.hosts[i] = h
                return
        self.hosts.append(h)

    def by_name(self, name: str) -> Optional[HostProfile]:
        for h in self.hosts:
            if h.name == name or h.host == name:
                return h
        return None