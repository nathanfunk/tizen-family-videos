#!/bin/bash
# Family Videos TV app: sign -> install on TV -> update Desktop copy + rpi copy -> relaunch.
# Single entry point so the Hermes approval gate treats it as one simple command.
#
# Machine/TV-specific values (token, IPs, passwords) live in deploy.local.sh
# (gitignored) — see deploy.local.sh.example.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source ./deploy.local.sh 2>/dev/null || true
: "${FV_AUTHOR_PW:?FV_AUTHOR_PW not set — copy deploy.local.sh.example to deploy.local.sh and fill it in}"
: "${FV_TV_HOST:?FV_TV_HOST not set — see deploy.local.sh.example}"
: "${FV_TV_TOKEN:?FV_TV_TOKEN not set — see deploy.local.sh.example}"
: "${FV_APP_ID:?FV_APP_ID not set — see deploy.local.sh.example}"

TIZEN_HOME="${TIZEN_HOME:-$HOME/tizen-studio}"
TIZEN_DATA="${TIZEN_DATA:-$HOME/tizen-studio-data}"
FV_TIZEN="${FV_TIZEN:-$TIZEN_HOME/tools/ide/bin/tizen.sh}"
export FV_TIZEN
FV_TVCTL="${FV_TVCTL:-$HOME/tv-test-venv/bin/samsungtv}"

# 1. blank machine-encrypted password attrs (WSL keyring is broken) so the CLI prompts via pty
sed -i 's#password="/home[^"]*"#password=""#g' "$TIZEN_DATA/profile/profiles.xml"

# 2. sign (python pty driver answers the password prompts)
# (stderr kept visible: the signer reports whether a personal icon was applied)
python3 "$SCRIPT_DIR/scripts/sign_famvids3.py" > /dev/null || \
  { echo "SIGN FAILED"; exit 1; }

# 3. install on the TV (sdb server may have died since the last session)
"$TIZEN_HOME/tools/sdb" connect "$FV_TV_HOST" > /dev/null 2>&1 || true
mv "$TIZEN_HOME/tools/ide/bin/out/"*.wgt out/famvids-deploy.wgt
"$FV_TIZEN" install -n famvids-deploy.wgt -- out | tee /tmp/fv-install.log | tail -3
grep -q "successfully installed" /tmp/fv-install.log || { echo "INSTALL FAILED"; exit 1; }

# 4. keep the Windows Desktop copy and the rpi dev copy in sync
cp out/famvids-deploy.wgt /mnt/c/Users/natha/Desktop/famvids.wgt
rsync -a tvapp/ rpi:~/projects/family-tv-app/tvapp/

# 5. relaunch the app on the TV
"$FV_TVCTL" --host "$FV_TV_HOST" --port 8002 --token "$FV_TV_TOKEN" app-run "$FV_APP_ID" | tail -1

echo "DEPLOY OK: $(git describe --always --dirty 2>/dev/null || echo norepo)"
