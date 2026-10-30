# Deploy times

Measured from `git push` to the end of the CD run (deploy job done, health check passed). Times are UTC, newest first.
Get the numbers with `gh run view <id> --json jobs -q '.jobs[]|"\(.name): \(.startedAt) -> \(.completedAt)"'`.
"Push to done" runs from the start of the CI run to the end of the deploy job.

## Why it was slow, and the fix

Until 2026-10-30 CD built two images, `linux/amd64` and `linux/arm64`. The amd64 build hit the layer cache (every layer `CACHED`, the app layer takes seconds). The arm64 build never did, so it rebuilt everything under QEMU emulation every time: system packages about 150 s, Chromium about 130 s, Python packages about 237 s. That was roughly 9 to 11 minutes of every deploy. Nothing runs the arm64 image (the Zephyrus is x86), so `291cc4c` dropped it.

| Commit | What changed | Push to done | CI | CD publish | CD deploy | Notes |
|---|---|---|---|---|---|---|
| `291cc4c` 2026-10-30 | Workflows only: amd64 build only, actions bumped to Node 24 majors | **1 m 19 s** | 42 s | 18 s | 11 s | First run without arm64. Everything cached. |
| `e42748e` 2026-10-30 | UI: sidebar, recipe sheet, light/dark toggle, new icon | 14 m 09 s | 2 m 50 s | 11 m 02 s | 9 s | arm64 still built from scratch. |
| `b36312f` 2026-10-30 | UI: first Apple HIG redesign | 17 m 40 s | 5 m 21 s | 10 m 48 s | 1 m 24 s | arm64 from scratch. |
| `aa8b67e` 2026-10-23 | `cd.yml` only (deploy from push runs only) | 19 m 05 s | 7 m 39 s | 9 m 55 s | 1 m 23 s | Workflow-only change, still a full arm64 rebuild. |
| `a46c4c0` 2026-10-16 | CI: pass the private-repo token to the Docker build | 16 m 46 s | 4 m 31 s | 10 m 58 s | 1 m 11 s | |
| `9885e10` 2026-10-16 | UI: resizable sidebar, tag expander | not deployed | 48 s (failed) | skipped | skipped | CI failed: the Docker build needed the private-repo token. Fixed by `a46c4c0`. |
| `f35146a` 2026-10-13 | UI only | 10 m 06 s | 59 s | 8 m 50 s | 8 s | |
| `0b543ba` 2026-10-13 | App code (entities, measures, notes import) | 12 m 15 s | 2 m 35 s | 9 m 24 s | 8 s | |
| `449f178` 2026-10-13 | App code (intake, meal case) | 12 m 59 s | 3 m 05 s | 9 m 33 s | 14 s | |
| `1af142e` 2026-10-13 | Dockerfile: Chromium in its own layer before the other pip requirements, plus app code (tags) | 16 m 47 s | 7 m 35 s (docker build 7 m 30 s, tests 17 s) | 7 m 41 s | 1 m 18 s | Cold: layer order changed, so every layer was rebuilt in CI and CD. |
| `10d686d` 2026-10-12 | UI only (app code), old Dockerfile | about 19 m (push about 23:30, last CD done 23:49:01) | 5 m 17 s | 12 m 52 s and 11 m 17 s | 1 m 28 s and 1 m 30 s | Two CD runs overlapped (this commit and `cbeaa0e`). Cold after the xauth Dockerfile change. Push time is approximate. |
| `cbeaa0e` 2026-10-12 | Dockerfile: install xauth, writable Chromium cache | about 20 m | 7 m 01 s | 11 m 26 s | 1 m 30 s | Cold (Dockerfile change). |
| `75c5ae9` 2026-10-12 | Shared AI hub for Ollama and Whisper (compose.hub.yaml) | 8 m 42 s | 2 m 06 s | 6 m 20 s | 9 s | |
| `bf0c22d` 2026-10-12 | First CI and CD | 17 m 09 s | 5 m 27 s | 9 m 52 s | 1 m 43 s | First build, nothing cached. |

## Reading the numbers

- **Deploy** is 8 to 14 s when only a small layer changed, and about 1 m 20 s when the pull has to fetch more. It was never the slow part.
- **CI** swings from 42 s to 7 m 39 s with the layer cache. The slow CI runs are the ones where the Docker build missed the cache.
- Next check: after the next push that changes only files under `app/`, expect a total of a few minutes. If CD publish takes longer than about 2 minutes, look for `CACHED` lines in the Build step log.
- The first push after any Dockerfile change is always slow (cold cache), so mark those as cold.
