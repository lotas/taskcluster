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

# $HOME IS VALIDATED AND NORMALISED FIRST, because both checks below are built
# out of it. An empty HOME turned the "under $HOME" pattern into `/*`, which
# trusts EVERY absolute NVM_DIR -- including another account's -- and a trailing
# slash produced `/home/research//*`, which rejects the legitimate
# `/home/research/alt-nvm` and then resets NVM_DIR to a `//` path.
_qf_home="${HOME:-}"
case "$_qf_home" in
  /*) ;;
  *) _qf_home="" ;;                    # unset, relative, or nonsense
esac
while [ -n "$_qf_home" ] && [ "$_qf_home" != "/" ] \
      && [ "${_qf_home%/}" != "$_qf_home" ]; do
  _qf_home="${_qf_home%/}"
done

if [ -z "$_qf_home" ]; then
  # NOTHING IS TOUCHED. Without a usable HOME there is no way to tell this
  # user's nvm from anyone else's, and guessing is how the wrong account's CLIs
  # get executed. The caller's own `command -v` check then fails with a message
  # that names the diagnostic.
  echo "agent-env.sh: HOME is unset or not absolute; PATH left alone" >&2
else
  # AN INHERITED `NVM_DIR` IS TRUSTED ONLY IF IT LIVES UNDER `$HOME`.
  #
  # This file's whole job is to find the CLIs belonging to the user we are
  # running AS, so a value pointing into someone else's home is not a hint, it
  # is a wrong answer. `sudo -H` normally scrubs it, but `sudo -E`, an
  # `env_keep` entry, or a hand-run `tick.sh` can all carry the operator's
  # `NVM_DIR=/root/.nvm` into a research shell -- and the symptom is the one
  # this file exists to prevent: "no `claude` on PATH" for an installed CLI.
  #
  # Honoured when it IS under $HOME, because a legitimately relocated nvm is set
  # that way in the user's own `~/.profile`.
  case "${NVM_DIR:-}" in
    "$_qf_home"/*) ;;                  # the user's own, possibly relocated
    *) NVM_DIR="$_qf_home/.nvm" ;;
  esac
  export NVM_DIR

  # EVERY VERSION DIRECTORY THAT ACTUALLY HOLDS ONE OF THE CLIs, newest last so
  # that prepending leaves the newest first.
  #
  # NOT simply the newest directory. `nvm install 24` does NOT migrate global
  # packages, so `v24/bin` routinely exists without `claude` while `v22/bin`
  # still has it -- and picking the newest by name alone then hides an installed
  # CLI, which is precisely the failure this file was written to fix. The two
  # CLIs may also legitimately live under different node versions.
  #
  # `nvm` itself is never called: it is a shell function that does not exist in
  # a script. A hardcoded version would break on the next `nvm install`.
  # `sort -V` so v24 beats v9; ascending, because each iteration prepends.
  #
  # A `while read` loop rather than `for` over `ls`, so a path containing a space
  # is one path.
  while IFS= read -r _qf_bin; do
    [ -n "$_qf_bin" ] || continue
    [ -x "$_qf_bin/claude" ] || [ -x "$_qf_bin/codex" ] || continue
    # GUARDED: the tick sources this and spawns children that source it again,
    # and an unguarded prepend grows PATH without bound.
    case ":$PATH:" in
      *":$_qf_bin:"*) ;;
      *) PATH="$_qf_bin:$PATH"; export PATH ;;
    esac
  done <<EOF
$(ls -d "$NVM_DIR"/versions/node/*/bin 2>/dev/null | sort -V)
EOF
  unset _qf_bin
fi
unset _qf_home

# The proxy, in the same place `phase0-setup.sh`'s prelude looks for it. Without
# it git and both CLIs bypass tinyproxy, nftables refuses the connection, and the
# failure reads as a credential problem: "Failed to connect to github.com port
# 443 after 5 ms".
# shellcheck disable=SC1091
[ ! -r "${HOME:-/nonexistent}/.profile.d-proxy" ] \
  || . "$HOME/.profile.d-proxy"
