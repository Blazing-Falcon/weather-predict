# Decision Log

Changes to the planning decisions and to the planned repo setup. Planning decisions 1 to 27 stand unless an entry here supersedes one.

| Date | Change | Why |
|---|---|---|
| 2026-11-19 | No `AGENTS.md` in the repo. Agent notes stay outside git. | juan's instruction |
| 2026-11-19 | The planning snapshot folder is not tracked in git. | juan's instruction. It stays read-only on disk. |
| 2026-11-19 | Dependencies are added in the step that first uses them, with exact pins, instead of all at step 0. | Smaller env per step, and the lockfile history shows when each tool came in. |
| 2026-11-19 | `DATA_ROOT` defaults to `./data` (gitignored) when unset. | `.env` on dev holds only the token. |
