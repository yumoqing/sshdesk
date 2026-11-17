"""会话编辑与管理的对话框：新增/修改/复制/删除/测试/导入，设置全部落盘可维护。"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
                               QWidget)

from .config import AppConfig, HostProfile, default_mono_font
from .session import SSHError, SSHSession


def _row(*widgets) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for x in widgets:
        lay.addWidget(x)
    return w


class HostDialog(QDialog):
    """新增 / 修改一个会话。"""

    def __init__(self, profile: Optional[HostProfile] = None, parent=None):
        if profile is None:
            profile = HostProfile(font_family=default_mono_font())
        super().__init__(parent)
        self.setWindowTitle("会话设置" if profile else "新建会话")
        self.setMinimumWidth(520)
        p = profile or HostProfile()
        self._orig = profile

        lay = QVBoxLayout(self)

        basic = QFormLayout()
        self.name = QLineEdit(p.name)
        self.name.setPlaceholderText("例如：doit 生产 / 16 测试")
        basic.addRow("名称", self.name)
        self.host = QLineEdit(p.host)
        self.host.setPlaceholderText("IP 或域名")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(p.port or 22)
        basic.addRow("主机", _row(self.host, QLabel("端口"), self.port))
        self.user = QLineEdit(p.user)
        basic.addRow("用户", self.user)
        lay.addLayout(basic)

        # ---- 认证 ----
        auth_box = QGroupBox("认证")
        ag = QFormLayout(auth_box)
        self.auth = QComboBox()
        self.auth.addItems(["密钥（免密登录）", "密码"])
        self.auth.setCurrentIndex(0 if p.auth == "key" else 1)
        ag.addRow("方式", self.auth)

        self.key_row = QWidget()
        kr = QHBoxLayout(self.key_row)
        kr.setContentsMargins(0, 0, 0, 0)
        self.key_path = QLineEdit(p.key_path or "~/.ssh/id_rsa")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_key)
        kr.addWidget(self.key_path, 1)
        kr.addWidget(browse)
        ag.addRow("密钥文件", self.key_row)

        self.key_pass = QLineEdit(p.key_passphrase)
        self.key_pass.setEchoMode(QLineEdit.Password)
        self.key_pass.setPlaceholderText("没有口令就留空")
        ag.addRow("密钥口令", self.key_pass)

        self.pass_row = QWidget()
        pr = QHBoxLayout(self.pass_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.password = QLineEdit(p.password)
        self.password.setEchoMode(QLineEdit.Password)
        show = QCheckBox("显示")
        show.toggled.connect(lambda on: self.password.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        pr.addWidget(self.password, 1)
        pr.addWidget(show)
        ag.addRow("密码", self.pass_row)

        self.hostkey = QComboBox()
        self.hostkey.addItems(["自动：首连信任并记录（TOFU）", "严格：必须与 known_hosts 一致"])
        self.hostkey.setCurrentIndex(0 if (p.hostkey or "auto") == "auto" else 1)
        ag.addRow("主机密钥", self.hostkey)

        self.remember = QCheckBox("记住密码（写入本地配置文件，仅当前用户可读）")
        self.remember.setChecked(p.remember_password)
        ag.addRow("", self.remember)
        lay.addWidget(auth_box)

        # ---- 跳板机 ----
        self.jump_box = QGroupBox("跳板机（可选）")
        self.jump_box.setCheckable(True)
        self.jump_box.setChecked(bool(p.jump_host))
        jg = QFormLayout(self.jump_box)
        self.jump_host = QLineEdit(p.jump_host)
        self.jump_host.setPlaceholderText("跳板机 IP / 域名")
        self.jump_port = QSpinBox()
        self.jump_port.setRange(1, 65535)
        self.jump_port.setValue(p.jump_port or 22)
        jg.addRow("主机", _row(self.jump_host, QLabel("端口"), self.jump_port))
        self.jump_user = QLineEdit(p.jump_user)
        jg.addRow("用户", self.jump_user)
        self.jump_auth = QComboBox()
        self.jump_auth.addItems(["密钥（免密登录）", "密码"])
        self.jump_auth.setCurrentIndex(0 if p.jump_auth == "key" else 1)
        jg.addRow("方式", self.jump_auth)
        self.jump_key = QLineEdit(p.jump_key_path or "~/.ssh/id_rsa")
        jg.addRow("密钥文件", self.jump_key)
        self.jump_password = QLineEdit(p.jump_password)
        self.jump_password.setEchoMode(QLineEdit.Password)
        jg.addRow("密码", self.jump_password)
        lay.addWidget(self.jump_box)

        # ---- 终端 ----
        term = QFormLayout()
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 40)
        self.font_size.setValue(p.font_size or 11)
        term.addRow("字号", self.font_size)
        lay.addLayout(term)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        test = QPushButton("测试连接")
        bb.addButton(test, QDialogButtonBox.ActionRole)
        test.clicked.connect(self._test)
        bb.accepted.connect(self._accept_save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.auth.currentIndexChanged.connect(self._sync_auth)
        self.jump_auth.currentIndexChanged.connect(self._sync_auth)
        self._sync_auth()

    # ---- 交互 ----
    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择私钥", os.path.expanduser("~/.ssh"))
        if path:
            self.key_path.setText(path)

    def _sync_auth(self) -> None:
        use_key = self.auth.currentIndex() == 0
        self.key_row.setVisible(use_key)
        self.key_pass.setVisible(use_key)
        self.pass_row.setVisible(not use_key)
        jkey = self.jump_auth.currentIndex() == 0
        self.jump_key.setVisible(jkey)
        self.jump_password.setVisible(not jkey)

    def profile(self) -> HostProfile:
        p = HostProfile(
            name=self.name.text().strip(), host=self.host.text().strip(),
            port=self.port.value(), user=self.user.text().strip(),
            auth="key" if self.auth.currentIndex() == 0 else "password",
            key_path=self.key_path.text().strip(), key_passphrase=self.key_pass.text(),
            password=self.password.text() if self.auth.currentIndex() == 1 else "",
            remember_password=self.remember.isChecked(),
            hostkey="auto" if self.hostkey.currentIndex() == 0 else "strict",
            font_size=self.font_size.value())
        if self.jump_box.isChecked():
            p.jump_host = self.jump_host.text().strip()
            p.jump_port = self.jump_port.value()
            p.jump_user = self.jump_user.text().strip()
            p.jump_auth = "key" if self.jump_auth.currentIndex() == 0 else "password"
            p.jump_key_path = self.jump_key.text().strip()
            p.jump_password = self.jump_password.text() if p.jump_auth == "password" else ""
        return p

    def _test(self) -> None:
        p = self.profile()
        if not p.host or not p.user:
            self.status.setText("先填主机和用户")
            return
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        self.status.setText("连接中…")
        QGuiApplication.processEvents()
        s = SSHSession(p)
        try:
            s.connect()
            rc, out, _ = s.exec("echo sshdesk-ok; uname -a || ver")
            self.status.setText("✅ 连接成功（%s）：%s" % (p.display(), out.strip().replace("\n", " ")[:160]))
        except SSHError as e:
            self.status.setText("❌ %s" % e)
        finally:
            s.close()
            QGuiApplication.restoreOverrideCursor()

    def _accept_save(self) -> None:
        p = self.profile()
        if not p.host:
            self.status.setText("主机不能为空")
            return
        if not p.name:
            p.name = "%s@%s" % (p.user or "user", p.host)
        self._result = p
        self.accept()

    def result_profile(self) -> HostProfile:
        return getattr(self, "_result", self.profile())


class SessionManager(QDialog):
    """会话列表：新增 / 修改 / 复制 / 删除 / 测试 / 导入导出。"""

    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("会话管理")
        self.resize(640, 460)
        self.cfg = cfg
        self.chosen: Optional[HostProfile] = None

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("按名称/主机/用户过滤…")
        self.search.textChanged.connect(self._fill)
        top.addWidget(self.search, 1)
        lay.addLayout(top)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._connect())
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        for text, fn in (("新建", self._add), ("修改", self._edit), ("复制", self._dup),
                         ("删除", self._del), ("测试", self._test), ("连接", self._connect)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        row2 = QHBoxLayout()
        for text, fn in (("导入 ~/.ssh/config", self._import_ssh), ("导入 JSON…", self._import_json),
                         ("导出 JSON…", self._export_json), ("打开配置文件位置", self._open_dir)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row2.addWidget(b)
        lay.addLayout(row2)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.reject)
        lay.addWidget(close)
        self._fill()

    # ---- 列表 ----
    def _fill(self) -> None:
        kw = self.search.text().strip().lower()
        self.list.clear()
        for h in self.cfg.hosts:
            if kw and kw not in ("%s %s %s %s" % (h.name, h.host, h.user, h.jump_host)).lower():
                continue
            item = QListWidgetItem("%-22s %s@%s:%d  %s%s" % (
                h.name or "(未命名)", h.user, h.host, h.port,
                h.auth == "key" and "[密钥] " or "[密码] ",
                ("跳板 %s" % h.jump_host) if h.jump_host else ""))
            item.setData(Qt.UserRole, h)
            self.list.addItem(item)

    def _current(self) -> Optional[HostProfile]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    # ---- 增删改 ----
    def _add(self) -> None:
        dlg = HostDialog(None, self)
        if dlg.exec() == QDialog.Accepted:
            self.cfg.upsert(dlg.result_profile())
            self.cfg.save()
            self._fill()

    def _edit(self) -> None:
        h = self._current()
        if not h:
            return
        dlg = HostDialog(h, self)
        if dlg.exec() == QDialog.Accepted:
            new = dlg.result_profile()
            idx = self.cfg.hosts.index(h)
            self.cfg.hosts[idx] = new
            self.cfg.save()
            self._fill()

    def _dup(self) -> None:
        h = self._current()
        if not h:
            return
        import copy
        c = copy.deepcopy(h)
        c.name = (h.name or h.host) + "-副本"
        self.cfg.hosts.append(c)
        self.cfg.save()
        self._fill()

    def _del(self) -> None:
        h = self._current()
        if not h:
            return
        if QMessageBox.question(self, "删除", "删除会话「%s」？" % (h.name or h.host)) != QMessageBox.Yes:
            return
        self.cfg.hosts.remove(h)
        self.cfg.save()
        self._fill()

    def _test(self) -> None:
        h = self._current()
        if not h:
            return
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        s = SSHSession(h)
        try:
            s.connect()
            rc, out, _ = s.exec("echo ok; hostname || echo")
            QMessageBox.information(self, "测试", "✅ %s\n%s" % (h.display(), out.strip()[:200]))
        except SSHError as e:
            QMessageBox.warning(self, "测试失败", str(e))
        finally:
            s.close()
            QGuiApplication.restoreOverrideCursor()

    def _connect(self) -> None:
        h = self._current()
        if not h:
            return
        self.chosen = h
        self.accept()

    # ---- 导入导出 ----
    def _import_ssh(self) -> None:
        path = os.path.expanduser("~/.ssh/config")
        if not os.path.isfile(path):
            QMessageBox.information(self, "导入", "没有找到 %s" % path)
            return
        added = parse_ssh_config(path)
        for h in added:
            if not self.cfg.by_name(h.name):
                self.cfg.hosts.append(h)
        self.cfg.save()
        self._fill()
        QMessageBox.information(self, "导入", "从 ssh config 导入 %d 条" % len(added))

    def _import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入会话 JSON", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            items = raw.get("hosts", raw if isinstance(raw, list) else [])
            n = 0
            for d in items:
                h = HostProfile.from_dict(d)
                if h.host:
                    self.cfg.hosts.append(h)
                    n += 1
            self.cfg.save()
            self._fill()
            QMessageBox.information(self, "导入", "导入 %d 条" % n)
        except Exception as e:                  # noqa: BLE001
            QMessageBox.warning(self, "导入失败", str(e))

    def _export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出会话", "sshdesk-hosts.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hosts": [h.to_dict() for h in self.cfg.hosts]}, fh,
                      ensure_ascii=False, indent=1)
        QMessageBox.information(self, "导出", "已导出到 %s" % path)

    def _open_dir(self) -> None:
        from .config import config_path
        p = config_path()
        QMessageBox.information(self, "配置文件", p)


def parse_ssh_config(path: str) -> List[HostProfile]:
    """把 ~/.ssh/config 的 Host 段转成会话（支持 HostName/User/Port/IdentityFile/ProxyJump）。"""
    out: List[HostProfile] = []
    cur: Optional[dict] = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition(" ")
            k = key.lower()
            if k == "host":
                if cur:
                    out.append(cur)
                cur = {"name": val.strip().strip('"'), "host": val.strip()}
                continue
            if cur is None:
                continue
            val = val.strip().strip('"')
            if k == "hostname":
                cur["host"] = val
            elif k == "user":
                cur["user"] = val
            elif k == "port":
                cur["port"] = int(val) if val.isdigit() else 22
            elif k in ("identityfile",):
                cur["key_path"] = val
            elif k == "proxyjump":
                parts = val.split("@")
                cur["jump_user"] = parts[0] if len(parts) > 1 else ""
                cur["jump_host"] = parts[-1].split(":")[0]
    if cur:
        out.append(cur)
    profiles = []
    for c in out:
        if c.get("name", "").startswith("*") or not c.get("host"):
            continue
        profiles.append(HostProfile(name=c["name"], host=c["host"],
                                    port=int(c.get("port", 22)),
                                    user=c.get("user", ""), auth="key",
                                    key_path=c.get("key_path", "~/.ssh/id_rsa"),
                                    jump_host=c.get("jump_host", ""),
                                    jump_user=c.get("jump_user", "")))
    return profiles