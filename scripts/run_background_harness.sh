#!/bin/zsh

# One-shot launchd wrapper. launchctl submit keeps a submitted job loaded;
# remove it after the Harness exits so a terminal decision cannot restart it.
set -u

if [[ -n "${AUTO_RESEARCH_LAUNCHD_LABEL:-}" ]]; then
  label="$AUTO_RESEARCH_LAUNCHD_LABEL"
else
  label="${1:?first argument must be the launchd label}"
  shift
fi
cleanup() {
  launchctl remove "$label" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"$@"
status=$?
exit "$status"
