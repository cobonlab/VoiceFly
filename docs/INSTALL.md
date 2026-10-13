# Install Voice Fly / 安装 Voice Fly

## 普通用户安装

1. 确认你的电脑是 **M 系列 Mac，系统为 macOS 26.4 或更新版本**。当前安装包不支持 Intel Mac 或更早的系统。
2. 从 [GitHub 发布页面](https://github.com/cobonlab/VoiceFly/releases/tag/v0.2.0-preview.4)下载 `VoiceFly-0.2.0-preview.4-arm64.pkg`。这是完整安装包；页面里的 `Source code` 和 `-source.tar.gz` 是给开发者的源码。
3. 双击安装包。如果提示无法验证开发者，先尝试打开，再到 **系统设置 → 隐私与安全性 → 仍要打开**，确认安装。本预览版没有苹果 Developer ID 签名和公证。只对你确认来自本仓库的安装包操作；不要关闭整个系统的安全检查。苹果说明：[打开未公证的软件](https://support.apple.com/en-us/102445)。如果系统没有这个选项，请提供具体提示和系统版本，以便排查；不要把所有拦截都当成同一种情况。
4. 按安装窗口完成操作。写入系统输入法位置时，macOS 会要求管理员密码或设备认证。如果已安装 Voice Fly，请先结束文字和语音输入，再切到 ABC。
5. **保存工作，然后手动注销 Mac 并重新登录。** 首次安装或完整升级需要重新打开用户会话。安装程序不会自动替你注销，可以等其他工作完成后再操作。
6. 在“应用程序”里打开 **Voice Fly**。选择习惯的输入方案，按首次使用页面的提示启用输入法。macOS 询问“允许启用 Voice Fly”时，点击允许。再从右上角输入法菜单选择 Voice Fly。
7. 按住 Control 说话；首次使用时允许 **Voice Fly Service** 使用麦克风。第一次启动会准备本地文字处理资源，可能等待约一分钟，设置窗口会显示加载状态。
8. 在普通文本框试打“你好”，再试一句语音。可以在 Voice Fly 设置中调整输入方案、语音键、麦克风和个人词库。

安装包包含文字输入法、设置窗口、语音服务、Python 运行环境和本地模型。不需要另外安装鼠须管、Python 或 Homebrew，不需要 OpenAI 密钥，也不需要购买苹果开发者会员。基础语音识别在本机进行，安装完成后不需要联网。

文件校验是可选的下载完整性检查：发布页面同时提供 `SHA256SUMS.txt`；开发者可用 `shasum -a 256` 对照。这个校验不是苹果签名或公证。

## English installation summary

Download the `.pkg` from [v0.2.0-preview.4](https://github.com/cobonlab/VoiceFly/releases/tag/v0.2.0-preview.4). The package supports Apple Silicon/macOS 26.4+ and contains all baseline runtime/model components. No developer tools, separate Squirrel installation, API key or Apple developer membership are needed. This preview is ad-hoc signed, without Developer ID or notarization. If macOS blocks opening, follow its per-app **Privacy & Security → Open Anyway** flow after confirming the source. Finish typing/dictation and select ABC before an upgrade. Save work, log out/in manually after full installation, then approve the input method and microphone. The installer will not log you out.

## What was checked

The extracted installer was checked on the maintainer's macOS 26.6.2 Apple Silicon Mac. Its six keyboard schemes produced the expected greeting in a temporary profile. The packaged service verified its five model files and ran silence inference plus text/phonetic self-tests with network, Homebrew, the old development directory and system Python blocked. No microphone was opened for those checks. Bundle integrity, minimum OS load commands and internal library links were also checked.

An independent new user's download/allow/install/logout/login/real-recording flow is **not yet verified**. This is an installer preview, not a universal compatibility claim. The preview targets one logged-in user and the startup volume; managed/multi-user deployment remains unverified. A fresh development-machine source build remains separate work; see [BUILD.md](BUILD.md).

## Installed components

| Component | Location |
| --- | --- |
| Input method | `/Library/Input Methods/VoiceFly.app` |
| Settings | `/Applications/Voice Fly.app` |
| Speech service | `/Applications/Voice Fly Service.app` |
| Login startup | `~/Library/LaunchAgents/com.simonli.voicefly.service.plist` |
| Personal words and settings | `~/Library/Application Support/VoiceFly/` |
| Rime configuration | `~/Library/Rime-VoiceFly/` |

Updates preserve personal dictionaries. The installer makes a private backup before switching components and attempts recovery if configuration deployment fails. Earlier product names are recognized only for migration; active components use Voice Fly. Review backups locally and never upload them as issue attachments.

The existing-install artwork update has a separate no-logout path (`scripts/build_installer.py --hot-appearance`). Finish the current input/dictation and temporarily choose ABC before installing. Only Voice Fly is restarted; choose it again through the native menu afterward. The maintainer confirmed the new icon, Chinese typing and dictation on macOS 26.6.2 Apple Silicon without closing other applications. First installation and general full-component upgrades retain the logout flow until independently validated; do not claim that all upgrades are already seamless.

Programmatic selection is not a working-input check: on macOS 26 it can return success while leaving the input controller inactive ([upstream report](https://github.com/rime/squirrel/issues/1162)). Validate native switching, actual Chinese typing, and dictation before accepting an update. Configuration and personal vocabulary changes already use deferred reload rather than replacing the input-method application.

## Remove the application

From a source checkout, run `sudo bash scripts/uninstall.sh`. The script disables the input method and login service, then archives applications in the user's backup folder. Personal words and settings remain available for recovery. The preview targets one logged-in user; managed multi-user installation still needs separate validation.

## Troubleshooting

Open Voice Fly's status/help tab to restart the speech service. Check the microphone permission in System Settings if typing works but speech does not. If the input source is missing, use the first-use activation button or add Voice Fly in macOS keyboard/input-source settings. Report the OS version, chip and reproducible steps using invented text; omit personal words, audio and raw logs.
