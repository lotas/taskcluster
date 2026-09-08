# The names of the three state files that BOTH `tick.sh` and `pause-issue.sh`
# read and write. Sourced, never executed.
#
# WHY THIS FILE EXISTS -- a failure that no test could have caught. `tick.sh`
# advances and reads these counters; `pause-issue.sh check` ZEROES them on an
# authorised close and then reads them back before it removes `PAUSE`. Both used
# to hardcode the literals with nothing linking them. Rename one on either side
# and `check` zeroes a path nobody reads, THE READ-BACK STILL PASSES -- it reads
# back exactly what it just wrote -- `PAUSE` is removed, and the real counter is
# still sitting at its threshold, so the first rejection of the resumed tick
# pauses again. That is the livelock the ordering in §4.3 exists to prevent,
# reintroduced by a rename, and invisible to both test suites because they poke
# the same literals the code does.
#
# A SHARED FILE, NOT A CROSS-REFERENCE COMMENT, because the coupling is a
# CORRECTNESS one whose violation is silent and self-confirming. A comment asks
# a future editor to remember; this makes the rename impossible to do by halves.
#
# BASENAMES ONLY. `$STATE` differs per caller (`QF_TICK_STATE`, `XDG_STATE_HOME`,
# a test's temporary directory), so the directory is the caller's business and
# the names are not.
QF_STATE_DISAGREE=consecutive-disagreements
QF_STATE_VERIFIER=consecutive-verifier-failures
QF_STATE_TARGET=last-reject-target
