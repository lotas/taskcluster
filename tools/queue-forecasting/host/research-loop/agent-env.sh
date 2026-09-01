# Sourced, never executed. Puts the agent CLIs and the proxy on PATH/env for a
# NON-INTERACTIVE shell.
#
# THE TRAP, which cost a deployment: `which claude` works when you ssh in and
# fails inside the tick, and both are correct. nvm appends its init to
# `~/.bashrc`, and Debian's `~/.bashrc` opens with
#
#     case $- in *i*) ;; *) return;; esac
#
# so a NON-INTERACTIVE shell returns before nvm is ever set up. `bash -lc` is a
# login shell but not an interactive one, so `~/.profile` runs, sources
# `~/.bashrc`, and that file immediately returns. The CLIs are installed, on
# disk, and unreachable -- which reads as "not installed".
#
# `phase0-setup.sh:147` already solved this for its own invocations with
# `NVM_PRELUDE`, and everything there goes through `run_research()`. Nothing in
# the research loop did, hence this file.
#
# WHY A FILE AND NOT A COPY IN EACH SCRIPT. `tick.sh` needs it for itself and for
# the leader it spawns; `install.sh` needs it to preflight-check the CLIs AS the
# research user. Two copies of a PATH computation drift, and the failure mode is
# the one above: silent, and indistinguishable from a missing install.
#
# `phase0-setup.sh` keeps its own copy on purpose -- it is the bootstrap and runs
# before this file is deployed to /srv. That duplication is bounded and visible:
# `install.sh on` preflights both CLIs through THIS file, so a divergence is
# caught at install time rather than at 3am.

# AN INHERITED `NVM_DIR` IS TRUSTED ONLY IF IT LIVES UNDER `$HOME`.
#
# This file's whole job is to find the CLIs belonging to the user we are running
# AS, so a value pointing into someone else's home is not a hint, it is a wrong
# answer. `sudo -H` normally scrubs it, but `sudo -E`, an `env_keep` entry, or a
# hand-run `tick.sh` can all carry the operator's `NVM_DIR=/root/.nvm` into a
# research shell -- and the symptom is the one this file exists to prevent:
# "no `claude` on PATH" for a CLI that is installed.
#
# Honoured when it IS under $HOME, because a legitimately relocated nvm is set
# that way in the user's own `~/.profile`.
case "${NVM_DIR:-}" in
  "$HOME"/*) ;;                       # the user's own, possibly relocated
  *) NVM_DIR="$HOME/.nvm" ;;
esac
export NVM_DIR

# THE NEWEST INSTALLED NODE, resolved by directory listing rather than by calling
# nvm. `nvm` is a shell FUNCTION that only exists after sourcing `nvm.sh`, so a
# script cannot call it, and hardcoding `v24.19.0` would break silently on the
# next `nvm install`. `sort -V` so v24 beats v9.
_qf_nvmbin="$(ls -d "$NVM_DIR"/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
if [ -n "$_qf_nvmbin" ]; then
  # NOT PREPENDED BLINDLY: the tick re-execs and spawns children that source this
  # again, and an unguarded prepend grows PATH without bound.
  case ":$PATH:" in
    *":$_qf_nvmbin:"*) ;;
    *) PATH="$_qf_nvmbin:$PATH"; export PATH ;;
  esac
fi
unset _qf_nvmbin

# The proxy, in the same place `phase0-setup.sh`'s prelude looks for it. Without
# it git and both CLIs bypass tinyproxy, nftables refuses the connection, and the
# failure reads as a credential problem: "Failed to connect to github.com port
# 443 after 5 ms".
# shellcheck disable=SC1091
[ ! -r "$HOME/.profile.d-proxy" ] || . "$HOME/.profile.d-proxy"
