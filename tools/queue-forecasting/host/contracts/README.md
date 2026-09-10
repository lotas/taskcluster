# contracts

- `*.json.in` — templates. `@BASELINE_HASH@` is unpinned, so `contract.validate`
  refuses one: a rule judging against nothing cannot be published by accident.
- `*.json` — published contracts, each pinned to a promoted `baseline_hash` and
  carrying its own `contract_hash`. Root-owned in the trusted checkout, read by
  the dispatcher and the evaluator, never written by them.
- `ACTIVE` — which published contract each target is judged by. One
  `<target> <contract_hash>` per line; `#` comments and blank lines allowed. An
  absent file, or a target with no line, means "chosen by scored-run usage". An
  entry that names no published contract **refuses the run** rather than falling
  back to usage (the `.json` is not deployed, or is not mode 0644 so the
  dispatcher cannot read it); two lines for one target keep the first.

**Contracts are never deleted.** `research-loop/frontier.py:load_contract` reads
an old contract's body to interpret the cohorts that were judged under it —
holdout overlap, metric ranks, gate sets — so removing v1 makes every historical
v1 result unreadable. The file is tracked, so a deploy would restore it anyway.

## Cutting over to a new contract

```
./instantiate-contract.sh contracts/wait_time.v2.json.in <baseline_hash> --activate
```

or edit `ACTIVE` by hand. Then **commit both files**: a setting that exists only
on one host is undone by the next deploy. `experiment.py plan` prints which
contract it resolved and whether `ACTIVE` or usage chose it. `--contract <hash>`
overrides `ACTIVE` for one run and changes nothing on disk.
