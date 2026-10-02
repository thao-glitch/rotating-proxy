#!/data/data/com.termux/files/usr/bin/bash
#
# Rotating Proxy -- Termux (Android) installer.
#
# The APK from the releases page runs this script inside Termux, or you
# can run it yourself:
#
#     curl -fsSL https://github.com/@REPO@/releases/download/@TAG@/rotating-proxy-install-termux.sh | bash
#
# @REPO@ and @TAG@ are substituted by the release workflow.
#
# What it does: installs python + git, clones the tagged source into
# ~/rotating-proxy, installs a `rotating-proxy` control command and
# starts the proxy headless (Android has no Tkinter, so this runs in
# --cli mode; the panel exists on desktop only).
#
set -euo pipefail

REPO="@REPO@"
TAG="@TAG@"
APP_HOME="$HOME/rotating-proxy"
LOG="$HOME/.rotating-proxy.log"
LAUNCHER="$PREFIX/bin/rotating-proxy"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- sanity
[ -n "${PREFIX:-}" ] && [ -x "${PREFIX}/bin/pkg" ] || \
    die "this installer must run inside Termux (pkg not found)"

say "Installing packages (python, git)"
pkg update -y >/dev/null 2>&1 || true
pkg install -y python git || die "package installation failed"
command -v python3 >/dev/null || die "python3 missing after install"

# ---------------------------------------------------------------- source
if [ -e "$APP_HOME/run.py" ]; then
    say "Already present: $APP_HOME (leaving it untouched)"
    ok "delete it first if you want a clean reinstall"
else
    say "Fetching Rotating Proxy $TAG"
    git clone --depth 1 --branch "$TAG" \
        "https://github.com/${REPO}.git" "$APP_HOME" \
        || die "could not clone https://github.com/${REPO} ($TAG)"
    ok "cloned into $APP_HOME"
fi

# ------------------------------------------------------------ control cmd
say "Installing the 'rotating-proxy' command"
cat > "$LAUNCHER" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Rotating Proxy control -- start/stop the headless proxy in Termux.
set -u
cd "$HOME/rotating-proxy" || exit 1
PIDF=".proxy.pid"
LOGF="$HOME/.rotating-proxy.log"

running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF" 2>/dev/null)" 2>/dev/null; }

case "${1:-start}" in
    start)
        if running; then
            echo "already running (pid $(cat "$PIDF"))"
            exit 0
        fi
        nohup python3 run.py --cli >>"$LOGF" 2>&1 &
        echo $! > "$PIDF"
        sleep 1
        if running; then
            echo "started  proxy: http://127.0.0.1:8888   log: $LOGF"
        else
            echo "failed to start -- last lines of $LOGF:"
            tail -n 10 "$LOGF" 2>/dev/null
            exit 1
        fi
        ;;
    stop)
        if running; then
            kill "$(cat "$PIDF")" && echo "stopped"
        else
            echo "not running"
        fi
        rm -f "$PIDF"
        ;;
    status)
        if running; then
            echo "running (pid $(cat "$PIDF"))  proxy: http://127.0.0.1:8888"
        else
            echo "not running"
        fi
        ;;
    log|logs)
        exec tail -f "$LOGF"
        ;;
    *)
        echo "usage: rotating-proxy start|stop|status|log"
        exit 2
        ;;
esac
EOF
chmod 755 "$LAUNCHER"
ok "$LAUNCHER installed"

# ------------------------------------------------------------------ start
say "Starting the proxy (headless mode)"
rotating-proxy start

# --------------------------------------------- optional: point shells at it
if [ -t 0 ]; then
    printf '\nAlso export http_proxy/https_proxy for new Termux shells?\n'
    printf '  (proxyctl env on -- reversible with: proxyctl env off) [y/N] '
    read -r answer || answer=""
    case "$answer" in
        y|Y|yes|YES)
            (cd "$APP_HOME" && ./proxyctl env on) || true
            ;;
    esac
fi

# --------------------------------------------------------------- wrap up
say "Done"
cat <<EOF
    proxy      http://127.0.0.1:8888
    control    rotating-proxy start|stop|status|log
    source     $APP_HOME
    log        $LOG

    Note: Android runs the proxy headless (no Tkinter panel here -- the
    desktop dashboard is Windows/macOS/Linux only).  Keep Termux alive
    (don't swipe it away) while the proxy is running, and exempt Termux
    from battery optimisation if Android kills it.
EOF
