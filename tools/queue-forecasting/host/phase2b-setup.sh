#!/usr/bin/env bash
# Phase 2b-1: provision the extractor's privilege domain. Run as root.
#
#   sudo ./phase2b-setup.sh discover     # read-only: what is and is not in place
#   sudo ./phase2b-setup.sh install      # idempotent
#
# WHAT THIS SCRIPT IS FOR. D15 says `qfd` may request an extraction and never
# holds the database credential. That is a claim about THIS HOST, and the only
# things that make it true are the ones below: a user with no privileges, a
# credential file nothing else can read, and a socket only the dispatcher can
# reach. `service.py`'s startup gate re-checks every one of them at each start,
# so a host that drifts fails loudly rather than quietly widening the domain.
#
# Written after `phase2-setup.sh` and following it: `would` for idempotence,
# `discover` before `install`, and every refusal naming the thing that fixes it.
# FAIL-CLOSED. The first version used `set -uo pipefail` only, so a failed
# `chown`, `chmod`, `daemon-reload` or `enable` was followed by a `discover` that
# exited 0 -- an install that reported success having not finished. `set -e` with
# an ERR trap that names the line is the difference between "installed" and
# "installed as far as it got".
#
# `would` returns 1 in dry-run and is only ever used as an `if` condition, where
# `set -e` does not fire, so the two compose.
set -Eeuo pipefail
trap 'echo "FATAL: ${BASH_SOURCE[0]}:${LINENO} failed (exit $?)" >&2; exit 1' ERR

TRUSTED="${TRUSTED:-/srv/queue-forecasting}"
EXTRACTOR="$TRUSTED/tools/queue-forecasting/host/extractor"
DSN_FILE="${DSN_FILE:-/etc/qf-extract/dsn}"
EXTRACTS_DIR="${EXTRACTS_DIR:-/var/lib/qf-extracts}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
DRY_RUN="${DRY_RUN:-0}"

FORBIDDEN_GROUPS=(docker qfheavy qfclient)

ok()   { echo "  ok    $*"; }
info() { echo "  $*"; }
warn() { echo "  warn  $*" >&2; }
die()  { echo "FATAL: $*" >&2; exit 2; }

would() {
  if [ "$DRY_RUN" = 1 ]; then echo "  WOULD $*"; return 1; fi
  echo "  $*"; return 0
}

need_root() { [ "$(id -u)" = 0 ] || die "run as root"; }

# --- discover --------------------------------------------------------------
discover() {
  echo "== discover =="
  local problems=0

  if id qfextract >/dev/null 2>&1; then
    ok "user qfextract exists (uid $(id -u qfextract))"
    # THE ASSERTION THAT MATTERS, and it is the inverse of a normal one: this
    # user must NOT be in these groups. The service refuses to start if it is,
    # but a host that cannot start its extractor is better diagnosed here.
    local held
    held="$(id -nG qfextract 2>/dev/null || true)"
    for group in "${FORBIDDEN_GROUPS[@]}"; do
      if printf ' %s ' "$held" | grep -q " $group "; then
        warn "qfextract is in '$group' and must not be: D15. Remove with"
        warn "  gpasswd -d qfextract $group"
        problems=$((problems + 1))
      else
        ok "qfextract is not in '$group'"
      fi
    done
  else
    info "user qfextract is absent (install will create it)"
    problems=$((problems + 1))
  fi

  if id qfd >/dev/null 2>&1; then
    ok "user qfd exists (uid $(id -u qfd)) -- the only permitted client"
  else
    warn "user qfd is absent; phase2-setup.sh creates it and 2b-1 needs its uid"
    problems=$((problems + 1))
  fi

  if [ -f "$DSN_FILE" ]; then
    local mode owner
    mode="$(stat -c %a "$DSN_FILE")"
    owner="$(stat -c %U:%G "$DSN_FILE")"
    # MODE **AND** OWNER. Checking the mode alone accepted a 0600 qfd:qfd
    # credential as good -- and qfd reading the DSN is the one thing D15 exists
    # to prevent. "Owner-only" is only a boundary once you know who the owner is.
    if [ "$owner" != "root:root" ]; then
      warn "$DSN_FILE is owned by $owner and must be root:root. A 0600 file is"
      warn "unreadable by everyone EXCEPT its owner, so the owner is the whole"
      warn "of the boundary. systemd reads it as root and hands the service a"
      warn "0400 copy; nothing else needs to read it ever."
      problems=$((problems + 1))
    elif [ "$mode" = "600" ] || [ "$mode" = "400" ]; then
      ok "$DSN_FILE is $mode $owner"
    else
      warn "$DSN_FILE is mode $mode and must be 600 or 400: any group or other"
      warn "bit means something besides systemd can read the DSN"
      problems=$((problems + 1))
    fi
    # NOT printed, obviously. Only its shape is reported.
    if grep -qE '^postgres(ql)?://' "$DSN_FILE"; then
      ok "$DSN_FILE looks like a PostgreSQL DSN"
    else
      warn "$DSN_FILE does not start with postgresql:// -- the service will"
      warn "refuse to start rather than discover it as a connection error"
      problems=$((problems + 1))
    fi
  else
    info "$DSN_FILE is absent. Create it by hand -- this script will not write a"
    info "credential, because a credential a setup script can generate is a"
    info "credential in a shell history:"
    info "    install -m 0600 -o root -g root /dev/null $DSN_FILE"
    info "    printf 'postgresql://forecast_experiment:PASSWORD@HOST/forecasting\\n' > $DSN_FILE"
    problems=$((problems + 1))
  fi

  # THE LIVE ROLE SETTINGS ARE NOT CHECKED HERE, and that is a correction.
  #
  # The first version ran `psql "$(cat "$DSN_FILE")" -tAc 'SHOW ...'`, which puts
  # the whole credential -- password included -- into the psql process's argv,
  # where any user on the host can read it out of /proc. A setup script that
  # leaks the DSN to defend the claim that only one process holds it is worse
  # than one that does not check.
  #
  # The check has moved to where the credential legitimately lives:
  # `service.py`'s `probe_database` runs at startup, and `ping` reports
  # `ready: false` with the reason. So the live-role verification happens in the
  # one process that is supposed to be able to do it, and this script tells you
  # how to read the answer.
  info "live role settings are checked by the service, not here -- reading them"
  info "from a script would put the DSN in a process argument. After install:"
  info "    sudo -u qfd python3 -c \"import json,socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/qf-extract/sock');s.sendall(b'{\\\"op\\\":\\\"ping\\\"}\\n');print(s.recv(65536).decode())\""
  info "    journalctl -u qf-extract -n 40"

  for unit in qf-extract.socket qf-extract.service; do
    if [ -f "$UNIT_DIR/$unit" ]; then ok "$unit installed"
    else info "$unit not installed"; problems=$((problems + 1)); fi
  done

  if [ -d "$EXTRACTS_DIR" ]; then
    ok "$EXTRACTS_DIR exists ($(stat -c '%a %U:%G' "$EXTRACTS_DIR"))"
  else
    info "$EXTRACTS_DIR absent (StateDirectory= creates it)"
  fi

  echo
  if [ "$problems" -eq 0 ]; then
    echo "== discover: nothing outstanding =="
  else
    echo "== discover: $problems item(s) outstanding =="
  fi
  return 0
}

# --- install ---------------------------------------------------------------
install_all() {
  need_root
  echo "== install =="

  id qfd >/dev/null 2>&1 || die "user qfd does not exist; run phase2-setup.sh first"
  local qfd_uid; qfd_uid="$(id -u qfd)"

  if ! id qfextract >/dev/null 2>&1; then
    if would "create system user qfextract"; then
      useradd --system --no-create-home --shell /usr/sbin/nologin qfextract \
        || die "useradd qfextract failed"
    fi
  else
    ok "user qfextract already exists"
  fi

  # Belt and braces against a host that drifted: remove the memberships D15
  # forbids rather than only reporting them. Removing a group from a service
  # account is safe; leaving it is the thing that is not.
  for group in "${FORBIDDEN_GROUPS[@]}"; do
    if getent group "$group" >/dev/null 2>&1 \
       && id -nG qfextract 2>/dev/null | tr ' ' '\n' | grep -qx "$group"; then
      if would "remove qfextract from '$group' (D15)"; then
        gpasswd -d qfextract "$group" >/dev/null || warn "could not remove"
      fi
    fi
  done

  [ -f "$DSN_FILE" ] || die "$DSN_FILE does not exist. Create it by hand; see
  discover. This script deliberately does not generate a credential."
  if would "tighten $DSN_FILE to 0600 root:root"; then
    chown root:root "$DSN_FILE" && chmod 0600 "$DSN_FILE"
  fi

  # THE ENVIRONMENT. Without it the unit's ExecStart names an interpreter that
  # does not exist, and the service cannot start at all.
  local envdir="$EXTRACTOR/env"
  [ -f "$envdir/pyproject.toml" ] || die "$envdir/pyproject.toml is missing"
  command -v uv >/dev/null 2>&1 || die "uv is not installed, and it is what
  builds the extractor's environment. Install it as root:
    curl -LsSf https://astral.sh/uv/install.sh | sh"
  if [ -f "$envdir/uv.lock" ]; then
    if would "sync $envdir/.venv from the committed lock"; then
      ( cd "$envdir" && uv sync --frozen --no-dev )
    fi
  elif [ "${ALLOW_UNLOCKED_ENV:-0}" = 1 ]; then
    # The escape hatch, and it is deliberately awkward to reach. Generating a
    # lock is how the FIRST one comes into existence; every install after that
    # should be using the committed one.
    warn "ALLOW_UNLOCKED_ENV=1: generating $envdir/uv.lock"
    if would "generate $envdir/uv.lock and sync"; then
      ( cd "$envdir" && uv lock && uv sync --frozen --no-dev )
      warn "COMMIT $envdir/uv.lock NOW, or the next host gets different versions"
    fi
  else
    # REFUSED, not warned.
    #
    # The previous version generated a lock and printed "now commit it". It was
    # generated on one host, the reminder was read, and the lock is still not in
    # the repository -- so this install path had produced exactly the situation it
    # was warning about, twice, while reporting success. A warning that has been
    # ignored once is documentation; a refusal is a control.
    #
    # What is at stake is small and real: two hosts installed a week apart get
    # different pyarrow versions, and their extracts differ in bytes for a reason
    # nobody can reconstruct. Published extracts are immutable, so nothing already
    # recorded changes -- but a difference nobody can explain is how an unexplained
    # difference gets into a comparison.
    die "$envdir/uv.lock is missing and is not in the repository.

  Generate it once, commit it, and re-run:
      sudo ALLOW_UNLOCKED_ENV=1 $0 install
      cp $envdir/uv.lock <your checkout>/host/extractor/env/uv.lock
      git add host/extractor/env/uv.lock && git commit

  Refusing rather than generating silently: the previous version generated one
  and asked for it to be committed, and it was not -- so the ask has become a
  refusal."
  fi
  if [ "$DRY_RUN" != 1 ]; then
    # Positive confirmation that the interpreter the unit names can import what
    # the service imports. Cheaper to find here than in the journal.
    "$envdir/.venv/bin/python" -c 'import pyarrow, psycopg' \
      || die "$envdir/.venv cannot import pyarrow and psycopg"
    PYTHONPATH="$TRUSTED/tools/queue-forecasting/host/shared" \
      "$envdir/.venv/bin/python" -c 'import extract_spec' \
      || die "the venv cannot import extract_spec from host/shared"
    ok "the venv imports pyarrow, psycopg and extract_spec"
  fi

  # THE ONE SUBSTITUTION. The unit ships with %%QFD_UID%% rather than a number,
  # because the uid is host-specific and a checked-in number would be wrong on
  # every host but one -- and wrong in the direction of admitting the wrong
  # client.
  for unit in qf-extract.socket qf-extract.service; do
    [ -f "$EXTRACTOR/$unit" ] || die "$EXTRACTOR/$unit is missing"
    if would "install $unit (QFD_UID=$qfd_uid)"; then
      sed "s/%%QFD_UID%%/$qfd_uid/g" "$EXTRACTOR/$unit" \
        > "$UNIT_DIR/$unit" || die "could not write $UNIT_DIR/$unit"
      chmod 0644 "$UNIT_DIR/$unit"
    fi
  done
  if grep -q '%%' "$UNIT_DIR/qf-extract.service" 2>/dev/null; then
    die "an unsubstituted %%placeholder%% remains in the installed unit"
  fi

  if would "reload systemd and enable the socket"; then
    systemctl daemon-reload
    # The SOCKET is enabled, not the service: that is what socket activation
    # means, and enabling the service instead would start the extractor at boot
    # whether or not anything ever asks it for an extract.
    systemctl enable --now qf-extract.socket
  fi

  # POSITIVE CONFIRMATION, not "systemctl enable returned 0". `enable --now` can
  # succeed while the socket fails to bind, and an install that says "listening"
  # without having asked is the shape of evidence this project keeps removing.
  if [ "$DRY_RUN" != 1 ]; then
    if systemctl is-active --quiet qf-extract.socket; then
      ok "qf-extract.socket is active"
    else
      die "qf-extract.socket is not active after enable --now. See
  systemctl status qf-extract.socket"
    fi
    if [ -S /run/qf-extract/sock ]; then
      ok "/run/qf-extract/sock exists ($(stat -c '%a %U:%G' /run/qf-extract/sock))"
    else
      die "/run/qf-extract/sock does not exist even though the socket unit is
  active. Check RuntimeDirectory= and ListenStream= in qf-extract.socket"
    fi
  fi

  echo
  info "the socket is listening; the service starts on the first request."
  info "Prove the round trip AS QFD, which is the only permitted client:"
  info "    sudo -u qfd python3 - <<'EOF'"
  info "    import json, socket"
  info "    s = socket.socket(socket.AF_UNIX); s.connect('/run/qf-extract/sock')"
  info "    s.sendall(json.dumps({'op': 'ping'}).encode() + b'\\n')"
  info "    print(s.recv(65536).decode())"
  info "    EOF"
  info ""
  info "and confirm the boundary holds -- these must BOTH fail:"
  info "    sudo -u qfd    cat $DSN_FILE"
  info "    sudo -u research cat $DSN_FILE"
}

main() {
  case "${1:-discover}" in
    discover) discover ;;
    install)  install_all; echo; discover ;;
    *) die "usage: $0 [discover|install]" ;;
  esac
}

main "$@"
