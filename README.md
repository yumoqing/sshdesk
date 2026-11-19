# sshdesk — Windows 上的 SSH / SFTP 客户端

一个自己用的桌面客户端：多标签终端 + 文件上传下载 + 跳板机 + 免密/密码登录 + 字号可调。
Windows 原生运行（PySide6），不依赖 PuTTY、不依赖 WSL。

## 功能

| 能力 | 说明 |
|---|---|
| 多标签 | 每个标签一条独立会话；标签可关闭、可拖动排序；左侧双击会话再开一个 |
| 终端 | paramiko 通道 + pyte 解析 + 自绘网格，支持 16 色/256 色、光标闪烁、窗口 resize 自动通知远端 |
| 字号 | 工具栏 A+/A-/A0（Ctrl+= / Ctrl+- / Ctrl+0），改一次全部标签生效并记住 |
| 认证 | **密钥（免密）**：Ed25519/ECDSA/RSA，支持密钥口令；**密码**：可选"记住密码" |
| 跳板机 | 在跳板机上开 `direct-tcpip` 通道再连目标，跳板机本身同样支持密钥/密码 |
| 文件传输 | 单栏远端 + 拖拽，上传/下载带**整批进度/速率/剩余时间**；**大文件断点续传**（中断/取消后从断点续上，已完整的自动跳过）；新建目录、删除、刷新、双击进目录 |
| 会话管理 | 新增/修改/复制/删除/测试连接，搜索过滤，**导入 `~/.ssh/config`**，导入/导出 JSON 备份 |
| 主机密钥 | 每会话可选 **自动（TOFU，首连记录+之后比对）** 或 **严格（必须与 known_hosts 一致）**；密钥变了直接拒绝并提示 |
| 滚动历史 | 滚轮 / Shift+PgUp/PgDn 回看最近 5000 行，右下角显示"历史 -N 屏" |

## 安装与运行

```bat
python -m pip install -e D:\work\repos\sshdesk
sshdesk                 :: 打开主窗口
sshdesk doit-prod       :: 直接连某个已保存的会话
sshdesk --list          :: 列出已保存会话
```

依赖：
- `PySide6-Essentials`（界面）、`pyte` + `wcwidth`（终端解析）
- **`appPublic`（`appPublic.sshx`）** —— SSH 连接层直接复用这个模块（内部是 asyncssh），
  支持 `jumpers` 多级跳板、`client_keys`/`password`，不用自己再包一层。
  它不在 PYTHONPATH 时，`sshdesk/session.py` 会自动往 `../apppublic`、`D:\work\repos\apppublic`
  找；也可以用环境变量指定：`set SSHDESK_APPPUBLIC=D:\work\repos\apppublic`。
- appPublic 的 `sshx` 还依赖 `asyncssh` 与 `jinja2`（`appPublic.log` 用到），装一下即可。

```bat
python -m pip install -e D:\work\repos\appPublic   :: 或者把仓库根加进 PYTHONPATH
```

## 打包成 exe

```bat
python tools/build_exe.py --onedir            :: 产物 dist\sshdesk\sshdesk.exe（推荐，双击即用）
python tools/build_exe.py --onefile --console :: 单文件（启动慢些，便于分发）
```

onedir 实测 **124 MB**（PySide6 占大头），包含 PySide6/asyncssh/paramiko/pyte/appPublic 全部依赖，
目标机不需要装 Python。冒烟：

```bat
dist\sshdesk\sshdesk.exe --selftest     :: 起窗口 1.2 秒后自动退出，rc=0 即正常
dist\sshdesk\sshdesk.exe --list         :: 列已保存会话
```

## 会话保存在哪

`%APPDATA%\sshdesk\hosts.json`（Windows 一般是 `C:\Users\<你>\AppData\Roaming\sshdesk\hosts.json`），
权限设成仅当前用户可读。结构：

```json
{
 "hosts": [
  {"name": "doit-prod", "host": "120.48.99.128", "port": 22, "user": "ymq",
   "auth": "key", "key_path": "~/.ssh/id_rsa", "key_passphrase": "",
   "jump_host": "", "jump_port": 22, "jump_user": "", "jump_auth": "key",
   "jump_key_path": "", "font_size": 11, "remember_password": false}
 ],
 "font_size": 11, "font_family": "Consolas", "show_file_panel": true
}
```

- **密码默认不写盘**；勾了"记住密码"才会存（明文，靠文件权限保护；不需要就别勾）。
- 所有字段都能在「会话管理 → 修改」里改，改完立刻落盘；也可以直接编辑 JSON 后重启。
- 会话列表支持 `导入 ~/.ssh/config`，把现有的 `Host`/`HostName`/`User`/`Port`/`IdentityFile`/`ProxyJump` 一次转进来。

## 测试

```bat
python tests/test_client.py
```

本机起两个 **paramiko 写的假 SSH 服务器**（一个当目标、一个当跳板，`direct-tcpip` 真转发），
客户端走 **appPublic.sshx**，两套用例**全部通过**：`python tests/test_client.py`（15 项端到端）+
`python tests/test_resume.py`（2 项断点续传专项，单独成文件：它会真搬几 MB、反复开关进度框，
混在长流程里容易踩到与本功能无关的 PySide 事件队列老问题）：

```
✅ 密码登录 + exec                  0.15s
✅ 密钥登录（免密）+ exec            0.12s
✅ 错误密码被拒                      0.09s
✅ 跳板机 direct-tcpip              0.18s     ← 客户端经跳板开通道，服务端确认收到转发
✅ SFTP 上传/下载/列表/建目录         0.10s     ← 上传后 sha256 一致，远端目录真建出来
✅ 交互 shell 往返                  0.16s
✅ 多标签 + 字体缩放 + 文件面板       2.86s     ← 2 个标签、字号 11→18 cell 高度变大、目录加载出来
```

`tests/test_resume.py`：

```
✅ 大文件上传/下载续传 · 跳过 · 截断    0.29s     ← 4MB 传一半取消 → 再传从断点续、sha256 一致；
                                               已完整的跳过、本地脏尾巴被截掉
✅ 面板续传 / 跳过 / 拖出续传          0.69s     ← 1MB 续到 2MB 显示「续传 1」，再传显示「跳过 1」，
                                               拖出下载也走同一套断点
```

产物：`out/sshdesk.png`（整窗）、`out/terminal.png`（终端单独截图，能看到 `fake-ssh ready` 和 `$`）。

假服务器的实现见 `tests/fake_sshd.py`：认证、exec、极简 shell REPL、chroot 风格的 SFTP、转发。

## 授权（私有化售卖）

`sshdesk/license.py` 是独立的一层：**Ed25519 签名 + 机器指纹 + 功能位 + 到期 + 心跳宽限**。
不依赖界面，可以整块搬到别的产品（只换 `APP` 与公钥）。

三层分工：

| 文件 | 干什么 |
|---|---|
| `sshdesk/license.py` | 客户端：验签、指纹比对、到期/宽限判定、`has(feature)` / `allow(limit, n)`、后台上报心跳 |
| `tools/license_tool.py` | 签发方：`keygen` 生成密钥、`machine` 看本机机器码、`issue` 出 `.lic`、`verify`/`inspect` 核验 |
| `tools/license_server.py` | 演示/自用授权服务：校验签名与指纹、按 `license_id` 吊销，回心跳（真运营要补 TLS、吊销表、限速） |

日常流程（三步，都不上客户机）：

```bat
:: 1) 只做一次：生成签发密钥（私钥落在 ~/.sshdesk-license/，不进仓库，丢了只能换公钥重发）
python tools/license_tool.py keygen

:: 2) 客户发来机器码（界面「授权 → 复制机器码」，或 sshdesk --license）
python tools/license_tool.py issue --org 某某公司 --days 365 ^
    --features sftp,resume --max-sessions 5 ^
    --machine S1-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx-xxxxxxxxxxxxxxxx-xx ^
    --heartbeat https://license.example.com/hb --out 某某公司.lic

:: 3) 客户装授权：界面「授权 → 粘贴授权…」整段粘贴，或命令行
sshdesk --activate 某某公司.lic
sshdesk --license            :: 查状态（机器码 / 到期 / 模式），客服排障用
```

口径与边界（改之前先读 `license.py` 顶部注释）：

- **签名覆盖除 `sig` 外的全部字段**（规范化 JSON），手改一个字就验签失败，不靠"藏"。
- **指纹三取二**：系统 UUID / 主网卡 MAC / 系统盘序列号，三项都要对上；license 里留空的那项不参与比对。
  换网卡或换盘 → 重新签发，不会误锁。
- **没有授权文件 = 30 天试用**，试用最多 2 个会话；到期后仍可开 1 个会话取数据（不把客户数据锁死）。
- **心跳不是"必须在线"**：`interval_days` 之后界面提示该联网，`grace_days` 用完才降级到 1 个会话；
  从安装那刻起就享受同样宽限，纯内网客户装完能正常跑。
- **时钟回拨**（记录历史最大值，回退超 6 小时）判异常，用来抬高"改系统时间续命"的成本，不是安全边界。
- 当前真正执行的门禁是 **会话数上限**（`open_tab` 里一行 `license.allow("max_sessions", …)`）；
  功能位已随授权下发，要用在别处只需一行 `if not self.license.has("jump"): …`。

⚠️ 这套是**提高拷贝成本**，不是防破解。真要守住核心，得让核心不在客户机上（见 README 之外的交付形态讨论）。
PyArmor/Nuitka 之类只影响"读代码"的成本，与 license 无关。

## 已知边界

- 终端是自绘的最小实现：全屏程序（vim/htop）能用，但鼠标上报、真彩色（24bit）、滚动历史回看还没做。
  已知限制：pyte 缩**行**会清屏，`TerminalView._resize_screen` 里做了内容保存重放来兜底（换字号/拉窗口不会丢字）。
- 会话改字号/窗口大小后会通知远端新 pty 尺寸，但远端程序重绘前可能短暂错行，正常现象。
- 只做了 SFTP v3 的常用操作（列目录/上传/下载/建目录/删除）；保留权限/时间戳未做，目录下载（递归拉整棵树）未做。
  断点续传按「大小 + mtime」判断：目标比源短就从断点续；一样长且不比源旧就跳过（所以「改完但字节数没变」的文件会被跳过，要强制重传先把目标删掉）。
  传输不设总超时，只有「单块 120 秒无进展」才算链路卡死——120MB 走 1Mbps 也能传完。
- 跳板机只支持一级；`ProxyCommand`、ssh-agent 转发未实现（密钥文件与默认 agent 认证可用）。
- 主机密钥：`auto` 模式是 TOFU（首次连接时**先连上再记录**，存在极短的信任窗口），`strict` 模式才做拒绝；
  没有提供逐条编辑 known_hosts 的界面，需要的话直接改 `%APPDATA%\sshdesk\known_hosts`。
- 滚动历史基于 pyte 的 HistoryScreen（5000 行），不支持搜索/选中复制历史行。

## macOS

界面和逻辑与 Windows/Linux 同一份（bricks .ui + bricks_qt），只需装依赖即可跑：

```bash
brew install python@3.12
python3 -m venv ~/.venvs/sshdesk && . ~/.venvs/sshdesk/bin/activate
pip install -e .                      # PySide6-Essentials / appPublic.sshx / pyte 都会装上
python -m sshdesk                     # 直接起窗口
```

- 配置目录走 macOS 规范：`~/Library/Application Support/sshdesk/`（hosts.json / known_hosts）
- 终端默认等宽字体：macOS `Menlo`（Windows Consolas / Linux DejaVu Sans Mono），可在会话里改
- 打包成 .app：`python tools/build_exe.py`（PyInstaller 在 macOS 上 `--windowed` 直接产出 `dist/sshdesk.app`）
  首次打开如被 Gatekeeper 拦，右键 → 打开，或 `xattr -dr com.apple.quarantine dist/sshdesk.app`
- 用 `~/.ssh/id_rsa` 免密连 kymoz 这类主机没问题（asyncssh 直接用系统密钥）
- CI 在 `macos-latest` 上跑 `.ui` 构建校验 + 原生控件冒烟，作为"macOS 原生可用"的持续证据

### 在 Mac 上实测（远程，经跳板机）

```bash
# 1) 传代码（在 Windows/WSL 侧执行；Mac 无 brew，别指望 pip 直连 PyPI 快速装）
tar czf - --exclude=out --exclude=dist --exclude=__pycache__ bricks-qt sshdesk \
  | ssh -J ymq@kymoz.com ymq@<mac> 'mkdir -p ~/code && tar xzf - -C ~/code'
tar czf - --exclude=__pycache__ appPublic | ssh -J ymq@kymoz.com ymq@<mac> 'mkdir -p ~/code/apppublic && tar xzf - -C ~/code/apppublic'

# 2) 环境（Mac 上 /usr/local/bin/python3.12；非交互 ssh 的 PATH 里没有它）
/usr/local/bin/python3.12 -m venv ~/.venv-sshdesk && . ~/.venv-sshdesk/bin/activate
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple PySide6-Essentials pyte wcwidth json5 paramiko asyncssh jinja2

# 3) 验收（同一套用例，Windows/Linux/macOS 完全一致）
export PYTHONPATH=~/code/bricks-qt:~/code/sshdesk:~/code/apppublic
python -m bricks_qt.cli check sshdesk/ui/session.ui --strict
python tests/native_smoke.py            # 打印 "native smoke ok: ... 在 darwin 可用"
```

图形验证用 `win.grab()` 存 PNG（ssh 会话里 `screencapture` 会因为缺"屏幕录制"权限失败）。
