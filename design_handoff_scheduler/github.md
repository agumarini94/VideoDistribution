repo: agumarini94/VideoDistribution
branch: main

## Last sync
date: 2026-09-08T13:57:11Z

### Updated in this project
- Modeled scheduling UI's job states (scheduled/queued/processing/published/failed) directly on `app/models.py::JobStatus`.
- Reflected multi-account/multi-client structure from `app/models.py::Account` (name-based client grouping) in the Client workspace switcher.
- Platform set drawn from `app/publishers/*` (Instagram, Facebook, TikTok, YouTube, X) plus Pinterest, which the backend doesn't support yet.

## Screen map
| Screen (Scheduler.dc.html) | Repo source |
| --- | --- |
| Queue, retry action | `app/models.py::Job`, `dashboard/api.py` (`/api/jobs`, `POST /api/jobs/{id}/retry`) |
| Connected accounts | `app/models.py::Account`, `scripts/add_account.py`, `scripts/authorize_*.py` |
| Analytics | `dashboard/api.py` (`/api/stats`) |
| Composer per-platform fields | `app/publishers/youtube.py` (shorts/playlist), platform payload shapes in `README.md` |
