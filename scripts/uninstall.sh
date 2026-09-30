#!/bin/bash
# Remove active applications by archiving them. Personal dictionaries stay local.
set -euo pipefail
[ "$(/usr/bin/id -u)" = 0 ] || { echo 'Run with sudo to archive the system input method.' >&2; exit 1; }
user=$(/usr/bin/stat -f%Su /dev/console)
[ "$user" != root ] && [ "$user" != loginwindow ] || exit 1
uid=$(/usr/bin/id -u "$user")
user_home=$(/usr/bin/dscl . -read "/Users/$user" NFSHomeDirectory | /usr/bin/sed 's/^NFSHomeDirectory: //')
backup="$user_home/Library/Application Support/VoiceFly/Backups/uninstall-$(/bin/date +%Y%m%d-%H%M%S)"
/bin/mkdir -p "$backup"
/bin/chmod 700 "$backup"
agent="$user_home/Library/LaunchAgents/com.simonli.voicefly.service.plist"
/usr/bin/sudo -u "$user" /bin/launchctl bootout "gui/$uid/com.simonli.voicefly.service" 2>/dev/null || true
front='/Library/Input Methods/VoiceFly.app/Contents/MacOS/VoiceFlyInput'
if [ -x "$front" ]; then /usr/bin/sudo -u "$user" "$front" --disable-input-source; /usr/bin/sudo -u "$user" "$front" --quit; fi
/usr/bin/sudo -u "$user" /usr/bin/killall VoiceFlySettings 2>/dev/null || true
for p in '/Library/Input Methods/VoiceFly.app' '/Applications/Voice Fly.app' '/Applications/Voice Fly Service.app' "$agent"; do
 [ ! -L "$p" ] || { echo 'Refusing a linked install location.' >&2; exit 1; }
 [ ! -e "$p" ] || /bin/mv "$p" "$backup/"
done
/usr/sbin/chown -R "$uid" "$backup"
echo 'Applications archived. Your personal words and settings are retained.'
