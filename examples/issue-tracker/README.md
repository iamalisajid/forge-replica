# Example: issue tracker replica

Output of `forge run http://127.0.0.1:4001/ --quick --mutations test-account --heldout demo/issue-tracker/heldout`
against the demo app in `demo/issue-tracker` (run `20261015-173704-5767`, 9.4 min, $1.51).

- `replica.bundle`: the generated repository (commits: G1 baseline, builder round 1, delivery; tag `forge/partial`).
- `verification.json`: the verification report.
- `report.txt`: `forge report` for the run (stage times and cost).

Verification: the pilot workflow and 4 of the 5 held-out workflows (never shown to the builder) pass on the replica
with no route to the original. One held-out workflow (`empty-title-rejected`) fails, so the overall status is
`partial`. All checks pass: health, routes == contract, egress blocked, request audit, restart persistence,
immutable hashes, regenerate-and-diff.

Restore and run:

```sh
git clone replica.bundle issue-tracker && cd issue-tracker
PORT=3100 docker compose up --build      # then open http://127.0.0.1:3100
```
