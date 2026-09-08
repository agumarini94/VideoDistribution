# Handoff: Postline — Social Scheduling UI

## Overview
UI design for a social media scheduling tool ("Postline") serving both individual creators and agencies managing multiple clients. Covers calendar/queue scheduling, a multi-platform composer, approvals, analytics, media library, connected accounts, and client-workspace switching. Backs onto the `distribution-engine` repo (agumarini94/VideoDistribution) — a Celery/FastAPI backend that already publishes to Instagram, Facebook, TikTok, YouTube and X, with a job state machine and a minimal read-only dashboard.

## About the Design Files
`Scheduler.dc.html` is a **design reference** — an interactive HTML/JS prototype (not production code to copy verbatim). It demonstrates layout, states and interactions with mock data. The task is to **recreate this design in the target codebase's environment** — likely as new routes/views on top of `dashboard/api.py` and `dashboard/static/` (or a proper SPA if the dashboard is upgraded), following that codebase's existing conventions — not to ship the HTML file itself.

To view it: open `Scheduler.dc.html` directly in a browser (all styles are inline, one file, no build step).

## Fidelity
**High-fidelity.** Colors, type, spacing and states below are final; recreate them precisely using whatever component/styling system the target app already uses (or plain CSS matching these tokens if none exists).

## Design Tokens
- Font — headings: `"Barlow Condensed", system-ui, sans-serif`, weight 600. Body: `"Barlow", system-ui, sans-serif`. Loaded via Google Fonts.
- Background: `#f2f2f3`. Surface (inputs/cards fill where used): `#e9e9ea`. Text: `#1d1f20`.
- Accent (single accent, "steel blue"): base `#5980a6`, hover `#597ea3` / dark step `#416180`, deepest `#2c455d` / `#1d2d3d`.
- Divider/border: `rgba(29,31,32,0.16)` (hairline, 1px, square corners — never rounded).
- Status colors: scheduled `bg #eef6ff / text #2c455d`; queued `bg #e7e7ea / text #424244`; processing `bg #d6ebff / text #416180`; published `bg #eef6ff / text #1d2d3d`; failed `bg #f5e7e7 / text #7a3030`.
- Icons: Lucide (stroke-width 1.5), loaded from `https://unpkg.com/lucide@latest/dist/umd/lucide.js`, rendered via `lucide.createIcons()`.
- Visual language: "blueprint" wireframe style — square corners everywhere (no border-radius), hairline 1px borders, small "+" crosshair registration marks at the corners of the calendar grid and analytics stat cards. No drop shadows, no filled card backgrounds except the solid accent-filled primary buttons.

## Screens / Views
All screens share a persistent **left sidebar** (216px, nav items: Calendar / Queue / Approvals / Analytics / Media library / Connected accounts / Clients) and a **top bar** (client-workspace switcher dropdown on the left, current screen title centered/left, "+ New post" primary button on the right, always visible).

1. **Calendar** — Month grid (7-col grid, weekday header row, each day cell shows up to 2 post chips colored by status + "+N more") and an Agenda view (grouped by date, each row: time · platform badge · caption · client name · status pill). Toggle between the two via a segmented control top-right.
2. **Queue** — Filter chips (All/Scheduled/Queued/Published/Failed) above a table: Platform, Post (caption, truncated), Client, Scheduled (date+time), Status pill, and a Retry action (icon+label) shown only on failed rows.
3. **Composer** — Two-column layout. Left: platform toggle chips, caption textarea, media drop zone (placeholder), date+time inputs, Save draft / Add to queue buttons. Right: "Live preview" — one framed card per selected platform showing platform badge, character counter (turns red past that platform's limit), a striped media placeholder, and the caption text as it will appear.
4. **Approvals** — List of pending posts (thumbnail placeholder, platform badge, caption, client + scheduled date) each with Reject/Approve buttons. Reject opens a modal dialog asking for a reason (textarea) before confirming.
5. **Analytics** — 4 stat cards (scheduled this week, published/failed in 30 days, connected accounts) with corner registration marks, a 7-day bar chart of posts published, and a per-platform breakdown table (published/failed/last activity).
6. **Media library** — 4-column grid of media thumbnails (striped placeholder + monospace label for photo/video), each with filename, dimensions, and "used in N posts".
7. **Connected accounts** — List of platform connections for the active client (platform badge, handle, status pill: Connected / Needs reauthorization) plus a "Connect platform" button that opens onboarding.
8. **Clients** (workspace switcher) — Grid of client cards (initial badge, name, kind — Individual/Client, account count, scheduled count, "Switch to" button) plus an "Add client workspace" tile. Also reachable via the top-bar dropdown.
9. **Onboarding** — 3-step platform connection flow: (1) choose platform from a grid, (2) mock OAuth consent screen with an "Authorize access" button, (3) confirmation showing that platform's default posting time slots.

## Interactions & Behavior
- Sidebar nav item click → switches `screen` state, highlights the active item with solid accent fill.
- Client dropdown → click opens a floating menu of clients; selecting one filters all data (jobs, accounts, stats) to that client's scope everywhere in the app.
- Calendar Month/Agenda toggle is local state, does not affect data.
- Queue filter chips are single-select, filter the table client-side.
- Composer: toggling a platform chip adds/removes its live-preview card; caption changes update every preview's text and character counter live.
- Approvals: Approve is a no-op in this mock (would call the retry/publish API); Reject opens a dialog, Cancel closes it without action, "Send rejection" closes and would notify the client.
- Queue retry icon → calls the retry action, flips that job's status to "queued" (mirrors `POST /api/jobs/{id}/retry` in `dashboard/api.py`, which only works on `failed` jobs).
- Onboarding: choosing a platform advances to step 2; "Authorize access" advances to step 3 (mock OAuth — no real redirect); "Done" returns to Connected accounts.

## State Management
Suggested state shape (already implemented this way in the prototype's logic class):
- `screen`: current view id.
- `activeClientId`: scopes all data views.
- `calendarView`: 'month' | 'agenda'.
- `queueFilter`: status filter id.
- `composerPlatforms`, `composerCaption`, `composerDate`, `composerTime`: composer draft.
- `rejectDialogOpen`, `rejectId`, `rejectReason`: approvals reject modal.
- `onboardingStep`, `onboardingPlatform`: onboarding wizard.
- `jobs`: local override list (falls back to mock data) so retry/status changes are visible without a backend.

### Data mapping to the existing backend (agumarini94/VideoDistribution)
- Job → `app/models.py::Job` (`platform`, `payload`, `status` using the `JobStatus` enum: scheduled/queued/processing/published/failed, `scheduled_at`, `attempts`, `error_message`, `external_id`).
- Account/connected platform → `app/models.py::Account` (`platform`, `name`, `credentials`, `is_active`). Client workspaces are currently modeled informally via the `Account.name` convention (e.g. "Client X") — there is no formal Client entity yet; introducing one is recommended if the client-switcher UI is built for real.
- Analytics/queue reads → `dashboard/api.py` (`GET /api/jobs`, `GET /api/stats`).
- Retry action → `POST /api/jobs/{id}/retry` (only valid on `failed` jobs, returns 409 otherwise).
- **Pinterest** is included in this design's platform set per the product brief, but the backend has no Pinterest publisher yet — flagged as new backend work, not just UI.
- TikTok note: current backend only supports inbox-upload (draft), not direct publish — consider surfacing that distinction in the real composer/queue if desired.

## Assets
No real imagery — all photo/video placeholders are diagonal-stripe SVG patterns with a monospace caption (e.g. "product shot", "video file"). Replace with real thumbnails from R2/media storage (`app/storage.py`) when implementing.

## Files
- `Scheduler.dc.html` — the full interactive prototype (single file, inline styles, mock data + local state).
- `github.md` — record of the source repo this design was grounded in and the screen-to-source-file mapping.
