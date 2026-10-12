# Decision Log

Changes to the planning decisions and to the planned repo setup. Planning decisions 1 to 27 stand unless an entry here supersedes one.

| Date | Change | Why |
|---|---|---|
| 2026-11-19 | No `AGENTS.md` in the repo. Agent notes stay outside git. | juan's instruction |
| 2026-11-19 | The planning snapshot folder is not tracked in git. | juan's instruction. It stays read-only on disk. |
| 2026-11-19 | Dependencies are added in the step that first uses them, with exact pins, instead of all at step 0. | Smaller env per step, and the lockfile history shows when each tool came in. |
| 2026-11-19 | `DATA_ROOT` defaults to `./data` (gitignored) when unset. | `.env` on dev holds only the token. |
| 2026-11-19 | Flow settings: DIS `fast`, 33 km smoothing (was `medium`, 66 km). | Experiment 03: 2.5% lower area error, about 3 times faster. |
| 2026-11-19 | `imerg_lag_minutes` is the offset from the issue time to the start of the newest usable IMERG slot, so it must cover the 30 min slot length plus the latency after the slot ends. Starts at 360. | Taken literally, the planned "LAG = p90 latency" with "slot t - LAG" would use a slot that is not yet available live (latency is measured from the slot end). |
| 2026-11-19 | The climatology baseline uses the event probability per lead (`p1`, `p2`, `p3` per area, month and local hour), not the cell wet share. | The baseline must predict the same event as the model. |
