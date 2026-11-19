"""私有化授权（license）：Ed25519 签名 + 机器指纹 + 功能位 + 到期 + 心跳宽限。

改之前先读这几条：

- 私钥只在签发方手里（`tools/license_tool.py keygen` 生成到仓库外），本模块只内嵌公钥。
  公钥漏了无所谓，私钥漏了等于全部授权作废。
- 签名覆盖除 `sig` 外的**全部**字段，规范化 json（sort_keys + 紧凑分隔符 + UTF-8）后签名，
  所以往 license 里手改一个字都会验签失败，不需要额外防篡改手段。
- 机器指纹三取二：系统 UUID、主网卡 MAC、系统盘序列号。三项都要对上才算本机
  （只存 sha256 前 16 位，不存原值）。任一组件取不到时该位记 16 个 0，不参与比对，
  客户换网卡/换盘只需要重新签发，不会因为一个组件读到空就锁死。
- 心跳不是"必须在线"：带心跳地址的授权在最后一次心跳成功之后宽限 grace_days 天，
  宽限期内照常用；从安装时刻起也享受同样宽限，所以纯内网客户装完照样能跑。
- 时钟回拨只做粗检测（记录历史最大值，回退超过 6 小时判异常），作用是把"改系统时间续命"
  的成本抬高，不是安全边界。
- 不 import 任何 GUI/业务代码，整块可以搬到别的产品（只换 app 名与公钥）。

命令行看一眼当前状态：`python -m sshdesk.license`
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple

APP = "sshdesk"
VERSION = 1

# 签发方公钥（Ed25519，32 字节 hex）。换签发方 = 换这里 + tools/license_tool.py 的私钥。
PUBKEY_HEX = "fedda75130baa32f336e0019450db44bc3482563c502b039b64eb9fe6bc8afd7"

TRIAL_DAYS = 30
TRIAL_LIMITS = {"max_sessions": 2}
CLOCK_SKEW_TOL = 6 * 3600            # 允许的时钟回退（秒）
HEARTBEAT_TIMEOUT = 5

LICENSE_NAME = "license.lic"
STATE_NAME = "license_state.json"


# --------------------------------------------------------------------------- #
# 机器指纹
# --------------------------------------------------------------------------- #
def _sha16(value: str) -> str:
    return hashlib.sha256(("%s|%s" % (APP, value)).encode("utf-8")).hexdigest()[:16]


def _reg(path: str, name: str) -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg                                  # noqa: PLC0415
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return str(winreg.QueryValueEx(k, name)[0] or "").strip()
    except Exception:                                   # noqa: BLE001
        return ""


def _win_disk() -> str:
    """系统盘序列号：Disk\\Enum 的 0 项（注册表读，比 wmic/powershell 快几百毫秒）。"""
    return _reg(r"SYSTEM\CurrentControlSet\Services\Disk\Enum", "0")


def _linux_disk() -> str:
    import glob
    for pat in ("/sys/block/sd*/device/serial", "/sys/block/nvme*/device/serial",
                "/sys/block/vd*/serial"):
        for p in sorted(glob.glob(pat)):
            try:
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    v = fh.read().strip()
                if v:
                    return v
            except OSError:
                continue
    return ""


def _mac_serial() -> Tuple[str, str]:
    """macOS：ioreg 一次性拿到硬件 UUID 与整机序列号。"""
    import subprocess
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:                                   # noqa: BLE001
        return "", ""
    def grab(key: str) -> str:
        for line in out.splitlines():
            if key in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"')
        return ""
    return grab("IOPlatformUUID"), grab("IOPlatformSerialNumber")


def machine_components() -> Dict[str, str]:
    """原始三项：sys=系统 UUID / mac=主网卡 / disk=系统盘序列号。取不到给空串。"""
    sysid = disk = ""
    if sys.platform.startswith("win"):
        sysid = _reg(r"SOFTWARE\Microsoft\Cryptography", "MachineGuid")
        disk = _win_disk()
    elif sys.platform == "darwin":
        sysid, disk = _mac_serial()
    else:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    sysid = fh.read().strip()
                if sysid:
                    break
            except OSError:
                continue
        disk = _linux_disk()
    try:
        mac = "%012x" % uuid.getnode()
    except Exception:                                   # noqa: BLE001
        mac = ""
    return {"sys": sysid, "mac": mac, "disk": disk}


def machine_fingerprint(comps: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """哈希后的三项，可直接写进 license；取不到的位是空串。"""
    c = comps if comps is not None else machine_components()
    return {k: (_sha16(v) if v else "") for k, v in c.items()}


def _code_checksum(g: str, m: str, d: str) -> str:
    return hashlib.sha256(("%s:%s:%s" % (g, m, d)).encode()).hexdigest()[:2]


def machine_code(comps: Optional[Dict[str, str]] = None) -> str:
    """给客户看的机器码：S1-<sys>-<mac>-<disk>-<ck>，空位用 0 补齐，末尾两位校验防手抄错。"""
    fp = machine_fingerprint(comps)
    g, m, d = (fp["sys"] or "0" * 16), (fp["mac"] or "0" * 16), (fp["disk"] or "0" * 16)
    return "S1-%s-%s-%s-%s" % (g, m, d, _code_checksum(g, m, d))


def parse_machine_code(code: str) -> Dict[str, str]:
    """把机器码还原成指纹三项；格式或校验不对抛 ValueError。"""
    parts = [p.strip() for p in str(code or "").strip().replace(" ", "").split("-")]
    if len(parts) != 5 or parts[0].upper() != "S1":
        raise ValueError("机器码格式不对，应为 S1-16位-16位-16位-2位")
    g, m, d, ck = parts[1].lower(), parts[2].lower(), parts[3].lower(), parts[4].lower()
    for v in (g, m, d):
        if len(v) != 16:
            raise ValueError("机器码每组应为 16 位十六进制")
    if _code_checksum(g, m, d) != ck:
        raise ValueError("机器码校验位不对（可能抄错）")
    return {"sys": "" if g == "0" * 16 else g,
            "mac": "" if m == "0" * 16 else m,
            "disk": "" if d == "0" * 16 else d}


def fingerprint_match(lic_fp: Dict[str, str], cur_fp: Dict[str, str]) -> Tuple[bool, int]:
    """三项全中才算本机；license 里空缺的位不参与比对。返回 (是否匹配, 命中数)。"""
    known = [k for k in ("sys", "mac", "disk") if (lic_fp or {}).get(k)]
    if not known:
        return False, 0
    hit = sum(1 for k in known if (cur_fp or {}).get(k) == lic_fp[k])
    return hit == len(known), hit


# --------------------------------------------------------------------------- #
# 签名 / 验签
# --------------------------------------------------------------------------- #
def canonical(payload: Dict) -> bytes:
    """签名口径：除 sig 外全部字段，键排序，紧凑分隔符，UTF-8。改口径 = 全部旧授权失效。"""
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign(private_key, payload: Dict) -> str:
    import base64 as _b64
    return _b64.b64encode(private_key.sign(canonical(payload))).decode("ascii")


def verify_signature(payload: Dict, sig_b64: str, pubkey_hex: str = PUBKEY_HEX) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pk.verify(base64.b64decode(sig_b64 or ""), canonical(payload))
        return True
    except Exception:                                   # noqa: BLE001
        return False


def load_private_key(path: str):
    """私钥文件 = 32 字节 hex 一行（keygen 生成，权限 600，不入仓库）。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))


def public_hex(private_key) -> str:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def make_license(private_key, org: str, days: Optional[int] = None,
                 expire: str = "", features=(), limits: Optional[Dict] = None,
                 machine: Optional[Dict[str, str]] = None, heartbeat: Optional[Dict] = None,
                 license_id: str = "", issued_at: Optional[float] = None) -> Dict:
    """签发一份 license（dict）。machine 传 parse_machine_code() 的结果 = 绑定单机。"""
    now = time.time() if issued_at is None else float(issued_at)
    if not expire:
        span = int(days if days is not None else 365)
        expire = time.strftime("%Y-%m-%d", time.gmtime(now + span * 86400))
    payload = {
        "v": VERSION,
        "app": APP,
        "id": license_id or "L%s" % time.strftime("%Y%m%d%H%M%S", time.gmtime(now)),
        "org": str(org or ""),
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "expire": str(expire),
        "features": sorted({str(f) for f in (features or ())}),
        "limits": {str(k): int(v) for k, v in (limits or {}).items()},
        "machine": dict(machine or {}),
        "heartbeat": dict(heartbeat or {}),
    }
    payload["sig"] = sign(private_key, payload)
    return payload


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
@dataclass
class State:
    mode: str = "invalid"        # trial|licensed|grace|expired|grace_expired|machine_mismatch|invalid|clock_tampered
    ok: bool = False
    reason: str = ""
    org: str = ""
    license_id: str = ""
    expire: str = ""
    features: Set[str] = field(default_factory=set)
    limits: Dict[str, int] = field(default_factory=dict)
    days_left: Optional[int] = None        # 试用/授权剩余天
    offline_days_left: Optional[int] = None  # 心跳宽限剩余天

    def label(self) -> str:
        if self.mode == "licensed":
            return "已授权：%s · %s 到期" % (self.org or "客户", self.expire)
        if self.mode == "grace":
            return "已授权：%s · %s 到期（离线 %d 天，宽限剩 %d 天）" % (
                self.org or "客户", self.expire,
                max(0, (self.limits.get("_offline_days") or 0)), self.offline_days_left or 0)
        if self.mode == "trial":
            return "试用版 · 剩 %d 天 · 最多 %d 个会话" % (
                self.days_left or 0, self.limits.get("max_sessions", 0))
        if self.mode == "expired":
            return "授权已到期（%s）" % (self.expire or "试用期结束")
        if self.mode == "grace_expired":
            return "授权已到期：%d 天未连上授权服务器（宽限 %d 天已用完）" % (
                self.limits.get("_offline_days") or 0, self.limits.get("_grace_days") or 0)
        if self.mode == "machine_mismatch":
            return "授权与本机不匹配：%s" % (self.reason or "机器码不同")
        if self.mode == "clock_tampered":
            return "系统时间异常（已回拨），请校正时间后重启"
        return "授权无效：%s" % (self.reason or "验签失败")


class License:
    """一次实例 = 一份授权文件 + 一份本地状态文件。所有时间判断可注入 now() 便于测试。"""

    def __init__(self, path: Optional[str] = None, state_path: Optional[str] = None,
                 pubkey: str = PUBKEY_HEX, app: str = APP,
                 now: Optional[Callable[[], float]] = None,
                 trial_days: int = TRIAL_DAYS,
                 trial_limits: Optional[Dict] = None):
        from .config import config_dir
        self.app = app
        self.pubkey = pubkey or ""
        self.path = path or os.environ.get("SSHDESK_LICENSE_FILE") or \
            os.path.join(config_dir(), LICENSE_NAME)
        self.state_path = state_path or os.path.join(config_dir(), STATE_NAME)
        self._now = now or time.time
        self.trial_days = int(trial_days)
        self.trial_limits = dict(TRIAL_LIMITS if trial_limits is None else trial_limits)
        self._data: Optional[Dict] = None
        self._state = self._load_state()
        self._cached: Optional[State] = None

    # ---- 文件 ----
    def _load_state(self) -> Dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    @property
    def data(self) -> Optional[Dict]:
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._data = raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data or None

    def install(self, text: str) -> State:
        """把客户粘进来的 license 文本落盘并重算状态。"""
        obj = json.loads(text) if isinstance(text, str) else dict(text)
        if not isinstance(obj, dict):
            raise ValueError("授权内容不是 JSON 对象")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._data = None
        self._cached = None
        return self.state(refresh=True)

    # ---- 时钟 ----
    def _touch_clock(self, now: float) -> bool:
        """记录见过的最大时间，返回是否发生了可疑回退。"""
        self._state.setdefault("first_seen", int(now))
        seen = int(self._state.get("max_seen") or 0)
        if now > seen + 60:
            self._state["max_seen"] = int(now)
            self._save_state()
        return now < seen - CLOCK_SKEW_TOL

    # ---- 判定 ----
    def state(self, refresh: bool = False) -> State:
        if self._cached is not None and not refresh:
            return self._cached
        st = self._evaluate()
        self._cached = st
        return st

    def _evaluate(self) -> State:
        now = float(self._now())
        rolled_back = self._touch_clock(now)
        data = self.data

        if data is None:                                  # 没有授权文件 → 试用
            first = int(self._state.get("first_seen") or now)
            if rolled_back:
                return State(mode="clock_tampered", ok=False, reason="系统时间被回拨")
            left = self.trial_days - int((now - first) // 86400)
            limits = dict(self.trial_limits)
            if left <= 0:
                limits["max_sessions"] = 1            # 到期后仍能开一个会话取数据，但要用得买
                return State(mode="expired", ok=False, reason="试用期已结束",
                             limits=limits, days_left=0)
            return State(mode="trial", ok=True, features={"base"}, limits=limits,
                         days_left=left)

        if not verify_signature(data, data.get("sig", ""), self.pubkey):
            return State(mode="invalid", ok=False, reason="签名校验失败")
        if data.get("app") not in (None, self.app):
            return State(mode="invalid", ok=False, reason="授权不属于本产品")

        expired_label = str(data.get("expire") or "")
        base = State(mode="licensed", ok=True, org=str(data.get("org") or ""),
                     license_id=str(data.get("id") or ""), expire=expired_label,
                     features=set(data.get("features") or []),
                     limits={k: int(v) for k, v in (data.get("limits") or {}).items()})

        if rolled_back:
            return State(mode="clock_tampered", ok=False, reason="系统时间被回拨",
                         org=base.org, license_id=base.license_id, expire=base.expire,
                         features=base.features, limits=base.limits)

        fp = data.get("machine") or {}
        if fp:                                             # 不绑机器的授权 → 任何机器可用
            ok, hit = fingerprint_match(fp, machine_fingerprint())
            if not ok:
                base.mode, base.ok = "machine_mismatch", False
                base.reason = "三项指纹只命中 %d 项" % hit
                return base

        exp_ts = _parse_expire(expired_label)
        if exp_ts is None:
            base.mode, base.ok, base.reason = "invalid", False, "到期日格式不对"
            return base
        if now > exp_ts:
            base.mode, base.ok = "expired", False
            base.days_left = 0
            base.limits["max_sessions"] = 1        # 到期只留一个会话，不直接把人挡在门外
            return base
        base.days_left = int((exp_ts - now) // 86400)

        # 授权有效期与本地记录对齐（换授权文件时重置心跳计时）
        if self._state.get("license_id") != base.license_id:
            self._state.update({"license_id": base.license_id,
                                "installed_at": int(now), "last_ok": 0})
            self._save_state()

        hb = data.get("heartbeat") or {}
        url = str(hb.get("url") or "")
        if not url:
            return base                                    # 纯离线授权，到期即可
        grace = int(hb.get("grace_days") or 15)
        interval = int(hb.get("interval_days") or 7)
        last_ok = int(self._state.get("last_ok") or 0)
        anchor = max(last_ok, int(self._state.get("installed_at") or now))
        offline_s = max(0, int(now - anchor))
        offline_days = offline_s // 86400
        base.limits["_offline_days"] = offline_days
        base.limits["_grace_days"] = grace
        if offline_s > grace * 86400:
            base.mode, base.ok = "grace_expired", False
            base.offline_days_left = 0
            base.limits["max_sessions"] = 1
            return base
        base.offline_days_left = max(0, grace - offline_days)
        if offline_s > interval * 86400:
            base.mode = "grace"                            # 照常可用，界面提示该联网了
        return base

    # ---- 取值 ----
    def has(self, feature: str) -> bool:
        st = self.state()
        return bool(st.ok and ("*" in st.features or feature in st.features))

    def limit(self, name: str, default: Optional[int] = None) -> Optional[int]:
        """授权里的数量上限；没有该字段 = 不限（返回 default）。"""
        return self.state().limits.get(name, default)

    def allow(self, name: str, current: int) -> Tuple[bool, str]:
        """数量门禁：current >= 上限 → 拒绝，并给一句能直接显示的理由。"""
        cap = self.limit(name)
        if cap is None or cap <= 0:
            return True, ""
        if current < cap:
            return True, ""
        return False, "当前授权最多 %d 个（%s），需要更多请购买授权" % (cap, name)

    def machine_code(self) -> str:
        return machine_code()

    # ---- 心跳 ----
    def heartbeat(self, url: str = "", timeout: int = HEARTBEAT_TIMEOUT) -> Dict:
        """打一次心跳；成功就刷新 last_ok，失败只记录次数（不降级，降级由宽限期决定）。"""
        import urllib.request
        data = self.data or {}
        hb = data.get("heartbeat") or {}
        target = url or str(hb.get("url") or "")
        if not target:
            return {"ok": False, "error": "授权没有配置心跳地址"}
        body = json.dumps({"app": self.app, "license_id": data.get("id", ""),
                           "org": data.get("org", ""),
                           "machine": machine_fingerprint(), "ts": int(self._now())},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(target, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        now = float(self._now())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            info = json.loads(raw) if raw.strip().startswith("{") else {}
        except Exception as e:                             # noqa: BLE001
            self._state["hb_fail"] = int(self._state.get("hb_fail") or 0) + 1
            self._save_state()
            return {"ok": False, "error": str(e)[:200]}
        if info.get("ok") is False:
            return {"ok": False, "error": str(info.get("error") or "被授权服务器拒绝"),
                    "body": info}
        self._state.update({"last_ok": int(now), "hb_fail": 0})
        self._save_state()
        self._cached = None
        return {"ok": True, "body": info}

    def heartbeat_async(self, url: str = "", timeout: int = HEARTBEAT_TIMEOUT):
        """后台线程打心跳，绝不阻塞界面。"""
        import threading
        t = threading.Thread(target=lambda: self.heartbeat(url, timeout),
                             name="license-heartbeat", daemon=True)
        t.start()
        return t


def _parse_expire(text: str) -> Optional[float]:
    """到期日按 UTC 当天 23:59:59 算（客户看到的是"当天还能用"）。

    用 calendar.timegm 而不是 time.mktime：后者按本机时区解释，UTC+8 会让到期的
    那个时刻偏 8~16 小时（实测过，客户会提前一天被锁）。
    """
    import calendar
    s = str(text or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            t = time.strptime(s, fmt)
            return float(calendar.timegm(t) + (86399 if len(s) == 10 else 0))
        except ValueError:
            continue
    return None


def _main(argv=None) -> int:
    """python -m sshdesk.license [license 文件]：打印本机机器码与授权状态。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    lic = License(path=argv[0] if argv else None)
    st = lic.state()
    print("机器码 : %s" % lic.machine_code())
    print("授权文件: %s%s" % (lic.path, "" if os.path.isfile(lic.path) else "（不存在，试用中）"))
    print("状态   : %s  [%s]" % (st.label(), st.mode))
    if st.features:
        print("功能位 : %s" % ", ".join(sorted(st.features)))
    if st.limits:
        print("限额   : %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(st.limits.items())))
    return 0 if st.ok else 1


if __name__ == "__main__":
    sys.exit(_main())