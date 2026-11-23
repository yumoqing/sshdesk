# Android 出包：现状与方案（2026-11-23 实测）

结论：**v0.1.0 不带 Android 包**。不是“懒得做”，是 Qt for Python 的 Android 通道和桌面版不是一套版本，必须单独立项。

## 一、实测到的事实（不是推测）

1. **Android 轮子只到 PySide6 6.9.3，且只有 cp311**
   `https://download.qt.io/official_releases/QtForPython/pyside6/` 下形如
   `PySide6-6.9.3-6.9.3-cp311-cp311-android_aarch64.whl` / `..._android_x86_64.whl`，
   没有 6.10 / 6.11 / 6.12 的 android 轮子。桌面版打的是 **PySide6 6.12.0 + CPython 3.12**。
   → Android 版必然落在 **PySide6 6.9.3 + CPython 3.11**，与桌面版是两个栈，代码要单独回归。

2. **`pyside6-android-deploy` 自己必须在 Python ≤ 3.11 下跑**
   在 py3.12 里执行直接报：
   `RuntimeError: [DEPLOY] Android deployment requires Python version 3.11 or lower. This is due to a restriction in buildozer.`

3. **工具链体积与来源**
   - NDK：要求 **r28c**（`android-ndk-r28c-linux.zip`，dl.google.com，实测 10 MB/s）
   - Android SDK：cmdline-tools + platform + build-tools（dl.google.com）
   - Python 3.11 源码由 p4a 交叉编译（python.org，实测 1.5 MB/s）
   - Qt for Android 轮子：**download.qt.io 实测只有 ~25 KB/s**，必须换镜像，aliyun 镜像实测 **4.3 MB/s**
     （`https://mirrors.aliyun.com/qt/official_releases/QtForPython/...`）
   合计下载约 1 GB，编译 10-20 分钟，gradle 首次构建还要拉 maven 依赖。

4. **纯 QWidget 界面 + 拖拽交互**
   sshdesk 是 bricks(.ui) → QWidget 渲染，SFTP 面板的核心交互是**拖入/拖出**。
   Android 没有桌面拖放语义，文件面板要改成“长按选择 → 分享/保存到…”，这不是换个打包命令能解决的。

5. **授权（pylice）**
   `pylice.py` 的 machine 指纹在 Linux 读 `/etc/machine-id`、在 Android 上需要单独实现（`Settings.Secure.ANDROID_ID` 之类）。
   pylice 支持“machine 留空 = 不绑机器”，可以先发不绑机的 lic 过渡，但正式发版前要定策略。

## 二、真要做时的最短路径

```bash
# 1) 解释器（buildozer 限制，必须 3.11）
uv python install 3.11

# 2) 工具链 venv（注意：装 6.9.3，不是桌面版 6.12）
uv venv -p 3.11 ~/work/venvs/pyside-android311
~/work/venvs/pyside-android311/bin/pip install "PySide6==6.9.3"
~/work/venvs/pyside-android311/bin/pip install -r \
  ~/work/venvs/pyside-android311/lib/python3.11/site-packages/PySide6/scripts/requirements-android.txt

# 3) 轮子走 aliyun 镜像（download.qt.io 太慢）
B=https://mirrors.aliyun.com/qt/official_releases/QtForPython
curl -O $B/pyside6/PySide6-6.9.3-6.9.3-cp311-cp311-android_aarch64.whl
curl -O $B/shiboken6/shiboken6-6.9.3-6.9.3-cp311-cp311-android_aarch64.whl

# 4) 工程目录：根目录放 main.py，把 sshdesk/ bricks_qt/ appPublic/ pylice.py 摊进去
#    （工具按目录扫 .py，不认已安装的 site-packages）
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
~/work/venvs/pyside-android311/bin/pyside6-android-deploy \
  --name sshdesk \
  --wheel-pyside PySide6-6.9.3-...-android_aarch64.whl \
  --wheel-shiboken shiboken6-6.9.3-...-android_aarch64.whl \
  -f --keep-deployment-files

# 5) 验证机：本机 KVM 的 Android-x86（192.168.122.77:5555，Android 9 / x86_64）
#    注意 ABI：x86 虚拟机要用 android_x86_64 轮子，真机用 android_aarch64，两个包得分别出。
adb connect 192.168.122.77:5555 && adb install -r *.apk
```

工程骨架已经搭好，留在 `~/work/android-sshdesk/`（`app/` 源码摊平版、`app-x86/`、`app-arm64/`、`wheels/` 四个轮子），
拿到 py3.11 后直接跑第 4 步即可，不用重新准备。

## 三、发版前的额外工作（非打包问题）

- [ ] 文件面板触屏化：拖拽 → 选择/分享/保存，`QDrag` 相关路径在 Android 上要 guard 掉
- [ ] 配置目录：`~/Library/Application Support`、`APPDATA`、`~/.config` 三分支在 Android 上要走 `QStandardPaths.AppConfigLocation`
- [ ] pylice 机器码：接 `ANDROID_ID`，或发“不绑机”lic
- [ ] 触屏尺寸下的 `.ui` 布局回归（当前按桌面 1280×800 设计）
- [ ] 图标/启动页/包名（`org.yumoqing.sshdesk`）与签名 keystore