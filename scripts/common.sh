# shellcheck shell=bash
# Shared helpers for install.sh and uninstall.sh (sourced, not run).
#
# The scripts can be started either by the user (root steps then go through
# sudo, or through su -c when sudo is missing or not allowed, as on ALT Linux)
# or as root (sudo, su -c, su -), in which case the per-user part is run as
# the user who called su/sudo.

# su -c without "-" keeps the user's PATH. As root, use the system directories
# only: a user-writable directory in PATH (~/.local/bin, ...) would let anything
# running as the user put its own "install" or "udevadm" in front of root.
# As the user, add the sbin directories, which ALT Linux leaves out.
SAFE_PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
USER_PATH="$PATH"
if [ "$(id -u)" = 0 ]; then
	PATH="$SAFE_PATH"
else
	case ":$PATH:" in
		*:/usr/sbin:*) ;;
		*) PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin" ;;
	esac
fi
export PATH

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

# as_root CMD [ARG...]: runs one command as root, asking for a password at most once.
as_root() {
	if [ "$(id -u)" = 0 ]; then
		"$@"
	elif command -v sudo >/dev/null 2>&1 && sudo -v 2>/dev/null; then
		sudo "$@"
	else
		# The command goes through a file run by bash: su -c hands its string to
		# root's login shell, which may not understand bash quoting.
		local f rc=0
		f="$(mktemp)"
		{ echo "PATH=$SAFE_PATH"; printf '%q ' "$@"; } > "$f"
		echo "   sudo is not available: enter the root password (su):"
		su -c "/bin/bash $f" || rc=$?
		rm -f "$f"
		return "$rc"
	fi
}

# target_user [NAME]: the user to install for when running as root.
target_user() {
	local u="${1:-}"
	[ -n "$u" ] || u="${SUDO_USER:-}"
	if [ -z "$u" ] && [ -n "${PKEXEC_UID:-}" ]; then
		u="$(getent passwd "$PKEXEC_UID" | cut -d: -f1)"
	fi
	if [ -z "$u" ] || [ "$u" = root ]; then
		# su keeps the login name of the terminal session
		u="$(logname 2>/dev/null || true)"
	fi
	if [ -z "$u" ] || [ "$u" = root ]; then
		u="$(stat -c %U "$ROOT" 2>/dev/null || true)"
	fi
	[ -n "$u" ] && [ "$u" != root ] && id "$u" >/dev/null 2>&1 || return 1
	printf '%s\n' "$u"
}

# run_as_user NAME CMD [ARG...]: runs a command as NAME with a clean user environment,
# including the session bus, so systemctl --user works if NAME is logged in.
run_as_user() {
	local u="$1" uid home
	shift
	uid="$(id -u "$u")"
	home="$(getent passwd "$u" | cut -d: -f6)"
	local envs=(HOME="$home" USER="$u" LOGNAME="$u" SHELL=/bin/bash PATH="$USER_PATH:$PATH")
	if [ -d "/run/user/$uid" ]; then
		envs+=(XDG_RUNTIME_DIR="/run/user/$uid")
		[ -S "/run/user/$uid/bus" ] && envs+=(DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus")
	fi
	if command -v runuser >/dev/null 2>&1; then
		runuser -u "$u" -- env -i "${envs[@]}" "$@"
	else
		su -s /bin/bash "$u" -c "$(printf '%q ' env -i "${envs[@]}" "$@")"
	fi
}

# user_systemctl ARG...: systemctl --user that explains instead of failing
# when there is no user session to talk to.
user_systemctl() {
	if ! systemctl --user "$@"; then
		echo "   could not run \"systemctl --user $*\" (no user session?)."
		echo "   Run it yourself after logging in."
	fi
}
