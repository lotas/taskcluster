#!/usr/bin/env bash
# Phase 2c Task 24: provision the evaluator's privilege domain. Run as root.
#
#   sudo ./phase2c-setup.sh discover     # read-only: what is and is not in place
#   sudo ./phase2c-setup.sh install      # idempotent
#
# WHY THIS SCRIPT EXISTS AT ALL, stated plainly because its absence was itself a
# finding. 2c-1 shipped `qf-eval.socket`, `qf-eval.service` and a committed
# `env/uv.lock` into the checkout, and NOTHING put them on the host. NC11 and
# NC9 (e)/(f) reported `void` rather than `pass`, which was the honest answer and
# also an unbounded one: a control that has never run is not a control. 2b-1's P1
# was the same shape one step earlier -- a unit naming an interpreter that no
# install step created.
#
# WHAT THE DOMAIN IS (D24, D28). `qfeval` holds NOTHING: no docker, no database
# credential, no network, no membership of any group that grants anything. Its
# whole authority is "read two immutable stores and one staged file, write one
# directory". So almost everything below is a NEGATIVE assertion -- this user is
# not in that group, this store is not writable by it, this environment carries no
# credential -- and `service.py`'s startup gate re-checks every one of them at
# each start, so a host that drifts fails loudly instead of quietly widening.
#
# THE ONE THING HERE THAT IS NOT A SYSTEMD DIRECTIVE is /var/lib/qf-eval, and it
# is the reason this script is not four lines of `install -m 0644`. Two uids meet
# in that directory: `qfd` creates `<run_id>/in/` and stages the untrusted
# prediction set, `qfeval` traverses in to read it and writes `out/`. So it
# cannot be a `StateDirectory=` of either unit -- systemd would create it owned by
# that unit's own user, and the other one would be locked out -- and the setgid
# bit on it is load-bearing rather than tidy: it is what puts the staged file in
# the evaluator's group without `qfd` being a member of that group.
#
# Written after `phase2b-setup.sh` and following it: `would` for idempotence,
# `discover` before `install`, `set -Eeuo pipefail` with a trap so a failed chown
# is not followed by a `discover` that exits 0, and every refusal naming the
# thing that fixes it.
#
# ENVIRONMENT:
#   TRUSTED        trusted checkout (default /srv/queue-forecasting)
#   RESEARCH_USER  the untrusted agent account (default: research)
#   DRY_RUN=1      print what install would do and change nothing
set -Eeuo pipefail
trap 'echo "FATAL: ${BASH_SOURCE[0]}:${LINENO} failed (exit $?)" >&2; exit 1' ERR

TRUSTED="${TRUSTED:-/srv/queue-forecasting}"
QF="$TRUSTED/tools/queue-forecasting"
EVALUATOR="$QF/host/evaluator"
CONTRACTS_DIR="${CONTRACTS_DIR:-$QF/host/contracts}"
EVAL_DIR="${EVAL_DIR:-/var/lib/qf-eval}"
EXTRACTS_DIR="${EXTRACTS_DIR:-/var/lib/qf-extracts}"
BASELINES_DIR="${BASELINES_DIR:-/var/lib/qf-baselines}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
TMPFILES_DIR="${TMPFILES_DIR:-/etc/tmpfiles.d}"
RESEARCH_USER="${RESEARCH_USER:-research}"
SOCK="${SOCK:-/run/qf-eval/sock}"
DRY_RUN="${DRY_RUN:-0}"

UNITS=(qf-eval.socket qf-eval.service)
# The staging root's required state, in one place: `discover` reports against it,
# `install` provisions it, and the tmpfiles config in the checkout must agree.
# Three copies of "2770 qfd qfeval" is how the fourth one gets forgotten.
EVAL_DIR_MODE=2770
EVAL_DIR_OWNER=qfd
EVAL_DIR_GROUP=qfeval

ok()   { echo "  ok    $*"; }
info() { echo "  $*"; }
warn() { echo "  warn  $*" >&2; }
die()  { echo "FATAL: $*" >&2; exit 2; }

would() {
  if [ "$DRY_RUN" = 1 ]; then echo "  WOULD $*"; return 1; fi
  echo "  $*"; return 0
}

need_root() { [ "$(id -u)" = 0 ] || die "run as root"; }

# `discover` is documented read-only and is run as root in practice, but the
# clauses that test a boundary have to BE another user to do it -- and `su` as a
# non-root caller prompts for a password, which in a setup script means a hang
# that looks like a check. So the clauses that need it say so and are skipped,
# rather than being silently reported as passing.
am_root() { [ "$(id -u)" = 0 ]; }

# --- the forbidden groups come FROM the gate, not from here ----------------
# THE LIST IS NOT REPEATED. `service.py` refuses to serve if `qfeval` is in any
# of `FORBIDDEN_GROUPS`, and a second hand-maintained copy of that tuple in a
# shell script is a copy that will disagree with it -- silently, and in the
# direction of provisioning a membership the service then refuses to run with.
# So the tuple is READ from the source it belongs to, and a parse that comes back
# empty is a refusal rather than a loop that does nothing.
forbidden_groups() {
  local src="$EVALUATOR/service.py" out
  [ -f "$src" ] || die "$src is missing; is TRUSTED=$TRUSTED right?"
  out="$(python3 - "$src" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "FORBIDDEN_GROUPS" for t in node.targets):
        value = ast.literal_eval(node.value)
        if not value:
            sys.exit("FORBIDDEN_GROUPS is empty")
        print(" ".join(str(v) for v in value))
        break
else:
    sys.exit("no FORBIDDEN_GROUPS assignment in the evaluator's service")
PY
)" || die "could not read FORBIDDEN_GROUPS from $src: $out"
  [ -n "$out" ] || die "FORBIDDEN_GROUPS parsed to nothing from $src"
  echo "$out"
}

# --- the mode of the staging root ------------------------------------------
# Extracted as a function with no side effects so it can be tested without root:
# the previous phase's lesson is that a check nobody can run is a check nobody
# has run. Takes what `stat -c '%a %U %G'` prints and returns the reason it is
# wrong, or nothing.
eval_dir_problem() {  # eval_dir_problem <mode> <owner> <group>
  local mode="$1" owner="$2" group="$3"
  if [ "$mode" != "$EVAL_DIR_MODE" ]; then
    # NAMED SEPARATELY when only the setgid bit is missing, because that is the
    # failure that does not look like one: 0770 qfd:qfeval lets the dispatcher
    # create the inbox and lets the evaluator traverse it, and then the staged
    # `predictions.parquet` inherits qfd's primary group instead of qfeval's and
    # the one file this directory exists for is the one thing unreadable.
    if [ "$mode" = "770" ] || [ "$mode" = "0770" ]; then
      echo "mode is $mode, not $EVAL_DIR_MODE: the SETGID bit is missing. A file"
      echo "created in a setgid directory takes the directory's group, which is"
      echo "how the staged prediction set reaches the evaluator without qfd"
      echo "being in its group. Without it the dispatcher refuses to stage."
      return 0
    fi
    echo "mode is $mode, not $EVAL_DIR_MODE"
    return 0
  fi
  if [ "$owner" != "$EVAL_DIR_OWNER" ]; then
    echo "owner is $owner, not $EVAL_DIR_OWNER: the dispatcher creates the"
    echo "per-run inbox, so it must own this directory"
    return 0
  fi
  if [ "$group" != "$EVAL_DIR_GROUP" ]; then
    echo "group is $group, not $EVAL_DIR_GROUP: the evaluator traverses in by"
    echo "group, and inherits it onto everything staged"
    return 0
  fi
  return 0
}

# --- discover --------------------------------------------------------------
discover() {
  echo "== discover =="
  local problems=0
  local groups; groups="$(forbidden_groups)"

  if id qfeval >/dev/null 2>&1; then
    ok "user qfeval exists (uid $(id -u qfeval))"
    local held; held="$(id -nG qfeval 2>/dev/null || true)"
    for group in $groups; do
      if printf ' %s ' "$held" | grep -q " $group "; then
        warn "qfeval is in '$group' and must not be (D24). The service refuses"
        warn "to start while it is. Remove with"
        warn "  gpasswd -d qfeval $group"
        problems=$((problems + 1))
      else
        ok "qfeval is not in '$group'"
      fi
    done
  else
    info "user qfeval is absent (install will create it)"
    problems=$((problems + 1))
  fi

  if id qfd >/dev/null 2>&1; then
    ok "user qfd exists (uid $(id -u qfd)) -- the only permitted client"
  else
    warn "user qfd is absent; phase2-setup.sh creates it and 2c needs its uid"
    problems=$((problems + 1))
  fi

  # THE STAGING ROOT, and this is the clause worth reading. Both uids and the
  # setgid bit, checked together: any one of the three being wrong breaks the
  # handover, and each breaks it in a different place.
  if [ -d "$EVAL_DIR" ]; then
    local state reason
    state="$(stat -c '%a %U %G' "$EVAL_DIR")"
    # shellcheck disable=SC2086
    reason="$(eval_dir_problem $state)"
    if [ -z "$reason" ]; then
      ok "$EVAL_DIR is $state"
    else
      warn "$EVAL_DIR: $reason"
      warn "install fixes it via $TMPFILES_DIR/qf-eval.conf"
      problems=$((problems + 1))
    fi
  else
    info "$EVAL_DIR is absent (install provisions it with systemd-tmpfiles)"
    problems=$((problems + 1))
  fi

  # THE INPUT STORES, checked as the evaluator: readable and NOT writable. The
  # gate refuses to start on a writable input store, so a host in this state has
  # an evaluator that will not run -- better said here than found in the journal.
  for label_dir in "extracts:$EXTRACTS_DIR" "baselines:$BASELINES_DIR" \
                   "contracts:$CONTRACTS_DIR"; do
    local label="${label_dir%%:*}" dir="${label_dir#*:}"
    if [ ! -d "$dir" ]; then
      warn "the $label store $dir does not exist; the evaluator refuses to start"
      problems=$((problems + 1))
      continue
    fi
    if ! id qfeval >/dev/null 2>&1; then
      info "the $label store $dir is $(stat -c '%a %U:%G' "$dir") (cannot test"
      info "as qfeval until the user exists)"
      continue
    fi
    if ! am_root; then
      info "the $label store $dir is $(stat -c '%a %U:%G' "$dir") (run as root"
      info "to test it as qfeval, which is the only way to know)"
      continue
    fi
    if ! su -s /bin/sh -c "test -r '$dir' && test -x '$dir'" qfeval; then
      warn "the $label store $dir is not readable by qfeval"
      problems=$((problems + 1))
    elif su -s /bin/sh -c "test -w '$dir'" qfeval; then
      warn "the $label store $dir is WRITABLE by qfeval. A judge that can edit"
      warn "an input it judges by is not a judge; the gate refuses to start."
      problems=$((problems + 1))
    else
      ok "the $label store $dir is readable and not writable by qfeval"
    fi
  done

  # THE CONTRACTS ARE THE RULE, so who can write them is the whole of NC9.
  if [ -d "$CONTRACTS_DIR" ]; then
    local count
    count="$(find "$CONTRACTS_DIR" -maxdepth 1 -name '*.json' | wc -l)"
    if [ "$count" -eq 0 ]; then
      info "no instantiated contract in $CONTRACTS_DIR (only templates). Task 18"
      info "needs a promoted baseline first: instantiate-contract.sh pins one."
      info "NC9 and NC11 stay void until at least one exists."
    else
      ok "$count instantiated contract(s) in $CONTRACTS_DIR"
    fi
    if ! am_root || ! id "$RESEARCH_USER" >/dev/null 2>&1; then
      info "$CONTRACTS_DIR is $(stat -c '%a %U:%G' "$CONTRACTS_DIR") (run as"
      info "root to test it as $RESEARCH_USER)"
    elif su -s /bin/sh -c "test -w '$CONTRACTS_DIR'" "$RESEARCH_USER"; then
      warn "$CONTRACTS_DIR is writable by $RESEARCH_USER. A contract the"
      warn "candidate can edit is not a contract (NC9)."
      problems=$((problems + 1))
    else
      ok "$CONTRACTS_DIR is not writable by $RESEARCH_USER"
    fi
  fi

  local py="$EVALUATOR/env/.venv/bin/python"
  if [ -x "$py" ]; then
    if "$py" -c 'import pyarrow, numpy' 2>/dev/null; then
      ok "the evaluator venv imports pyarrow and numpy"
    else
      warn "$py cannot import pyarrow and numpy; install re-syncs the closure"
      problems=$((problems + 1))
    fi
  else
    info "$py does not exist (install builds it from env/uv.lock)"
    problems=$((problems + 1))
  fi

  # UNIT DRIFT, which is a whole test file's worth of lesson in this tree:
  # `mirror-refresh` reset the checkout and left older units installed, so the
  # dispatcher ran new code under old configuration, silently. The comparison
  # function lives in phase2-setup.sh and is extracted rather than copied --
  # copied, its subtle first bug would be copied too.
  local checked_drift=0
  if declare -F unit_matches >/dev/null; then checked_drift=1; fi
  for unit in "${UNITS[@]}"; do
    if [ ! -f "$UNIT_DIR/$unit" ]; then
      info "$unit is not installed"
      problems=$((problems + 1))
      continue
    fi
    if [ "$checked_drift" != 1 ]; then
      # NOT `ok`, and this distinction is the whole lesson of the file that
      # tests `unit_matches`: an earlier version of that function had a bug that
      # made it pass on every input, and it "passed on identical files, which is
      # what a check that has stopped checking looks like from outside". Printing
      # "matches the checkout" when the comparison never ran is the same claim
      # from the same place.
      warn "$unit is installed and NOT COMPARED: unit_matches did not load from"
      warn "phase2-setup.sh, so this run cannot tell the installed unit from the"
      warn "checkout's. Run this script from host/, beside phase2-setup.sh."
      problems=$((problems + 1))
    elif ! unit_matches "$EVALUATOR/$unit" "$UNIT_DIR/$unit"; then
      warn "$UNIT_DIR/$unit differs from the checkout. The host is running this"
      warn "phase's code under another commit's configuration; install reinstalls."
      problems=$((problems + 1))
    else
      ok "$unit installed and matches the checkout"
    fi
  done
  if [ -f "$UNIT_DIR/qf-eval.service" ] && id qfd >/dev/null 2>&1; then
    local want installed
    want="$(id -u qfd)"
    installed="$(sed -n 's/^Environment=QFE_CLIENT_UID=\(.*\)$/\1/p' \
      "$UNIT_DIR/qf-eval.service")"
    if [ "$installed" = "$want" ]; then
      ok "QFE_CLIENT_UID is qfd's uid ($want)"
    else
      warn "QFE_CLIENT_UID is '$installed' and qfd is $want. A peer check"
      warn "against the wrong uid reads as though it names the dispatcher."
      problems=$((problems + 1))
    fi
  fi

  if [ -f "$TMPFILES_DIR/qf-eval.conf" ]; then
    ok "$TMPFILES_DIR/qf-eval.conf installed"
  else
    info "$TMPFILES_DIR/qf-eval.conf is not installed"
    problems=$((problems + 1))
  fi

  if systemctl is-active --quiet qf-eval.socket 2>/dev/null; then
    ok "qf-eval.socket is active"
    if [ -S "$SOCK" ]; then
      ok "$SOCK exists ($(stat -c '%a %U:%G' "$SOCK"))"
    else
      warn "$SOCK does not exist even though the socket unit is active"
      problems=$((problems + 1))
    fi
  else
    info "qf-eval.socket is not active"
    problems=$((problems + 1))
  fi

  dispatcher_namespace_report || problems=$((problems + 1))

  echo
  if [ "$problems" -eq 0 ]; then
    echo "== discover: nothing outstanding =="
  else
    echo "== discover: $problems item(s) outstanding =="
  fi
  return 0
}

# --- the dispatcher's view of the staging root ------------------------------
# MEASURED, NOT ASSUMED, and this is the one clause that could only have been
# written after reading how systemd builds a namespace. `ReadWritePaths=` is
# applied when the service STARTS: a directory created afterwards exists on the
# filesystem but is read-only inside the running dispatcher's mount namespace, so
# `install` can complete, `discover` can report `2770 qfd:qfeval`, and the first
# evaluation can still fail on mkdir with EROFS. The instruction "remember to
# restart qfd" is exactly the kind of evidence this project keeps deleting, so
# the writability is tested from INSIDE the namespace instead.
dispatcher_namespace_report() {
  local pid
  if ! am_root; then
    info "run as root to measure the dispatcher's view of $EVAL_DIR"
    return 0
  fi
  if ! systemctl is-active --quiet qf-dispatch 2>/dev/null; then
    info "qf-dispatch is not running; nothing to say about its namespace"
    return 0
  fi
  pid="$(systemctl show -p MainPID --value qf-dispatch 2>/dev/null || true)"
  if [ -z "$pid" ] || [ "$pid" = 0 ]; then
    warn "qf-dispatch is active with no MainPID; cannot check its namespace"
    return 1
  fi
  if ! command -v nsenter >/dev/null 2>&1; then
    # THE FALLBACK IS WEAKER AND SAYS SO. A directory whose status changed after
    # the service started is SUSPICIOUS, not proof: a re-run of systemd-tmpfiles
    # that changes nothing may still touch it.
    local dir_ctime started
    dir_ctime="$(stat -c %Z "$EVAL_DIR" 2>/dev/null || echo 0)"
    started="$(date -d "$(systemctl show -p ActiveEnterTimestamp --value \
      qf-dispatch)" +%s 2>/dev/null || echo 0)"
    if [ "$dir_ctime" -gt "$started" ] && [ "$started" -gt 0 ]; then
      warn "nsenter is not installed, so this is inference rather than a"
      warn "measurement: $EVAL_DIR changed after qf-dispatch started, so the"
      warn "running dispatcher may hold a read-only view of it. Restart it:"
      warn "  systemctl restart qf-dispatch"
      return 1
    fi
    info "nsenter is not installed; cannot measure the dispatcher's namespace"
    return 0
  fi
  if nsenter -t "$pid" -m -- su -s /bin/sh -c "test -w '$EVAL_DIR'" qfd \
     2>/dev/null; then
    ok "the RUNNING dispatcher can write $EVAL_DIR (measured in its namespace)"
    return 0
  fi
  warn "the running dispatcher CANNOT write $EVAL_DIR. Its mount namespace was"
  warn "built when it started, and ReadWritePaths= is applied then -- so a"
  warn "directory provisioned since is read-only to it and every evaluation"
  warn "would fail on mkdir. Restart it:"
  warn "  systemctl restart qf-dispatch"
  return 1
}

# --- install ---------------------------------------------------------------
install_all() {
  # DRY_RUN does not need root, and that is the point of it: `DRY_RUN=1 install`
  # is how an operator reads what this will do before letting it, and demanding
  # root to print a plan is how the plan goes unread.
  [ "$DRY_RUN" = 1 ] || need_root
  echo "== install =="
  local groups; groups="$(forbidden_groups)"

  id qfd >/dev/null 2>&1 || die "user qfd does not exist; run phase2-setup.sh first"
  local qfd_uid; qfd_uid="$(id -u qfd)"

  if ! id qfeval >/dev/null 2>&1; then
    if would "create system user qfeval"; then
      useradd --system --no-create-home --shell /usr/sbin/nologin qfeval \
        || die "useradd qfeval failed"
    fi
  else
    ok "user qfeval already exists"
  fi

  # Belt and braces against a host that drifted, as 2b-1 does: REMOVE the
  # memberships the gate forbids rather than only reporting them. Removing a
  # group from a service account that holds no privilege is safe; leaving one is
  # the thing that is not, and the service would refuse to start with it.
  for group in $groups; do
    if getent group "$group" >/dev/null 2>&1 \
       && id -nG qfeval 2>/dev/null | tr ' ' '\n' | grep -qx "$group"; then
      if would "remove qfeval from '$group' (D24)"; then
        gpasswd -d qfeval "$group" >/dev/null || warn "could not remove"
      fi
    fi
  done

  # THE STAGING ROOT, via systemd-tmpfiles rather than mkdir/chown here, for the
  # same reason qf-locks is: the config is a checked-in declaration of the
  # required state that runs again at every boot, so a host that is repaired by
  # hand into the wrong shape gets corrected rather than staying wrong.
  [ -f "$EVALUATOR/qf-eval.conf" ] || die "$EVALUATOR/qf-eval.conf is missing"
  if would "install qf-eval.conf and run systemd-tmpfiles"; then
    install -m 0644 "$EVALUATOR/qf-eval.conf" "$TMPFILES_DIR/qf-eval.conf"
    systemd-tmpfiles --create "$TMPFILES_DIR/qf-eval.conf" \
      || die "systemd-tmpfiles could not create $EVAL_DIR"
  fi
  if [ "$DRY_RUN" != 1 ]; then
    # POSITIVE CONFIRMATION of the mode, not "tmpfiles exited 0". The setgid bit
    # is the part that has no second chance: without it the dispatcher refuses to
    # stage, and with a silently-wrong owner it cannot create the inbox at all.
    local state reason
    state="$(stat -c '%a %U %G' "$EVAL_DIR")"
    # shellcheck disable=SC2086
    reason="$(eval_dir_problem $state)"
    [ -z "$reason" ] || die "$EVAL_DIR is $state after tmpfiles ran: $reason"
    ok "$EVAL_DIR is $state"
  fi

  # THE ENVIRONMENT. Without it ExecStart names an interpreter that does not
  # exist and the service cannot start at all -- 2b-1's P1, verbatim.
  local envdir="$EVALUATOR/env"
  [ -f "$envdir/pyproject.toml" ] || die "$envdir/pyproject.toml is missing"
  command -v uv >/dev/null 2>&1 || die "uv is not installed, and it is what
  builds the evaluator's environment. Install it as root:
    curl -LsSf https://astral.sh/uv/install.sh | sh"
  if [ -f "$envdir/uv.lock" ]; then
    if would "sync $envdir/.venv from the committed lock"; then
      ( cd "$envdir" && uv sync --frozen --no-dev )
    fi
  elif [ "${ALLOW_UNLOCKED_ENV:-0}" = 1 ]; then
    warn "ALLOW_UNLOCKED_ENV=1: generating $envdir/uv.lock"
    if would "generate $envdir/uv.lock and sync"; then
      ( cd "$envdir" && uv lock && uv sync --frozen --no-dev )
      warn "COMMIT $envdir/uv.lock NOW, or the next host gets other versions"
    fi
  else
    # REFUSED, not warned -- 2b-1 made this a refusal after the warning was
    # ignored twice, and two hosts installed a week apart with different pyarrow
    # versions is a difference nobody can later explain.
    die "$envdir/uv.lock is missing and is not in the repository.

  Generate it once, commit it, and re-run:
      sudo ALLOW_UNLOCKED_ENV=1 $0 install
      cp $envdir/uv.lock <your checkout>/host/evaluator/env/uv.lock
      git add host/evaluator/env/uv.lock && git commit"
  fi
  if [ "$DRY_RUN" != 1 ]; then
    local py="$envdir/.venv/bin/python"
    "$py" -c 'import pyarrow, numpy' \
      || die "$envdir/.venv cannot import pyarrow and numpy"
    PYTHONPATH="$QF/host/shared" "$py" -c \
      'import contract, baseline, extract_manifest' \
      || die "the venv cannot import contract, baseline and extract_manifest
  from host/shared -- which is what Environment=PYTHONPATH in the unit provides"
    # THE MODULE THE SERVICE ACTUALLY IMPORTS, not a proxy for it. `import
    # pyarrow` succeeding says nothing about `evaluate.py`'s own imports, and
    # `evaluate.py` is the file that grew a dependency between 2c-1 and 2c-2.
    PYTHONPATH="$QF/host/shared:$EVALUATOR" "$py" -c 'import evaluate, service' \
      || die "the venv cannot import the evaluator's own modules"
    ok "the venv imports pyarrow, numpy, host/shared and the evaluator itself"
  fi

  # THE ONE SUBSTITUTION, as in 2b-1: the unit ships %%QFD_UID%% because a
  # checked-in number is wrong on every host but one, and wrong in the direction
  # of admitting the wrong client.
  for unit in "${UNITS[@]}"; do
    [ -f "$EVALUATOR/$unit" ] || die "$EVALUATOR/$unit is missing"
    if would "install $unit (QFD_UID=$qfd_uid)"; then
      sed "s/%%QFD_UID%%/$qfd_uid/g" "$EVALUATOR/$unit" \
        > "$UNIT_DIR/$unit" || die "could not write $UNIT_DIR/$unit"
      chmod 0644 "$UNIT_DIR/$unit"
    fi
  done
  if [ "$DRY_RUN" != 1 ] && grep -q '%%' "$UNIT_DIR/qf-eval.service"; then
    die "an unsubstituted %%placeholder%% remains in the installed unit"
  fi

  if would "reload systemd and enable the socket"; then
    systemctl daemon-reload
    # THE SOCKET, not the service: that is what socket activation means, and
    # enabling the service would start a judge at boot whether or not anything
    # ever asks it for a verdict.
    systemctl enable --now qf-eval.socket
  fi

  if [ "$DRY_RUN" != 1 ]; then
    systemctl is-active --quiet qf-eval.socket \
      || die "qf-eval.socket is not active after enable --now. See
  systemctl status qf-eval.socket"
    ok "qf-eval.socket is active"
    [ -S "$SOCK" ] || die "$SOCK does not exist even though the socket unit is
  active. Check RuntimeDirectory= and ListenStream= in qf-eval.socket"
    ok "$SOCK exists ($(stat -c '%a %U:%G' "$SOCK"))"
    confirm_round_trip
  fi

  echo
  info "if the dispatcher's namespace check above asked for a restart, it means"
  info "the running qfd holds a read-only view of $EVAL_DIR and every"
  info "evaluation would fail on mkdir:"
  info "    systemctl restart qf-dispatch && sudo $0 discover"
}

# --- the round trip --------------------------------------------------------
# RUN, not printed. 2b-1 only printed these commands, and for a good reason there
# -- running them would have put the database DSN in a process argument. This
# domain holds no credential, so there is nothing to leak and no excuse for
# reporting "installed" without having asked the thing whether it works.
confirm_round_trip() {
  local reply
  reply="$(su -s /bin/sh -c "python3 - <<'EOF'
import json, socket
s = socket.socket(socket.AF_UNIX)
s.settimeout(10)
s.connect('$SOCK')
s.sendall(json.dumps({'op': 'ping'}).encode() + b'\n')
print(s.recv(65536).decode().strip())
EOF" qfd 2>&1)" || die "qfd could not ping the evaluator: $reply"
  echo "  ping: $reply"
  case "$reply" in
    *'"ok": true'*|*'"ok":true'*) ok "the evaluator answered qfd" ;;
    *) die "the evaluator did not answer ok. Its startup gate prints every
  refusal:  journalctl -u qf-eval -n 60" ;;
  esac
  case "$reply" in
    *'"can_evaluate": true'*|*'"can_evaluate":true'*)
      ok "it can evaluate (2c-2's implementation imported cleanly)" ;;
    *) warn "can_evaluate is not true: the service is up but cannot score."
       warn "journalctl -u qf-eval -n 60 names the import that failed." ;;
  esac
  # THE BOUNDARY, from the other side. A channel only qfd can open is a claim
  # about $RESEARCH_USER, and the claim is cheap to test.
  if id "$RESEARCH_USER" >/dev/null 2>&1; then
    if su -s /bin/sh -c \
       "python3 -c \"import socket; socket.socket(socket.AF_UNIX).connect('$SOCK')\"" \
       "$RESEARCH_USER" 2>/dev/null; then
      die "$RESEARCH_USER CAN open $SOCK. It is 0660 root:qfd and must be
  unreachable to the candidate; check SocketGroup= and whether $RESEARCH_USER
  has been added to qfd's group."
    fi
    ok "$RESEARCH_USER cannot open $SOCK"
  fi
}

# The drift comparison, extracted from phase2-setup.sh rather than copied. Its
# first implementation had a bug that made it pass on every input, so a second
# copy is a second chance to have that bug. Missing extraction is not fatal --
# `discover` reports one fewer thing rather than refusing to run at all.
_load_unit_matches() {
  local setup; setup="$(dirname "${BASH_SOURCE[0]}")/phase2-setup.sh"
  [ -f "$setup" ] || return 0
  # shellcheck disable=SC1090
  source <(sed -n '/^unit_matches()/,/^}/p; /^_unit_key_filter()/,/^}/p' \
    "$setup") || return 0
}

main() {
  _load_unit_matches
  case "${1:-discover}" in
    discover) discover ;;
    install)  install_all; echo; discover ;;
    *) die "usage: $0 [discover|install]" ;;
  esac
}

main "$@"
