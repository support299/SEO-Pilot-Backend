# Agent API — for AI agents (Claude, Codex, ...) working on customer sites

An agent **reads** a business's data and **proposes** changes. It can never approve,
publish, or undo anything: every proposal lands in the Execution queue as
*Awaiting approval*, and an agency admin approves and publishes it in the app.

Base URL: `https://seopilot.theservicepilot.com/api/v1/agent/`
Auth header on every call: `Authorization: Agent <key>`

## One-time setup (agency admin, on the server)

```bash
cd ~/SEO-Pilot-Backend && source venv/bin/activate
export DJANGO_SETTINGS_MODULE=config.settings.prod
# 1. Mark yourself as an agency admin (once)
python manage.py shell -c "from django.contrib.auth import get_user_model as g; u=g().objects.get(email='YOU@example.com'); u.is_agency_admin=True; u.save()"
# 2. Create a key. It is printed once; store it in the agent's secret store, never in git.
python manage.py create_agent_key "claude-daily" --admin-email YOU@example.com
```
Revoke a key any time in Django admin → Agent keys → "Revoke selected keys".

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `businesses/` | All customer businesses (id, name, website, customer, `wordpress_connected`, `publishing_enabled`) |
| GET | `businesses/{id}/context/` | Site-health findings, open issues, top queries/pages, open proposals |
| GET | `businesses/{id}/wordpress/pages/?type=page\|post` | WordPress pages/posts (ids needed to propose) |
| GET | `businesses/{id}/local-visibility/` | Google Maps rank per tracked keyword (visibility score, average rank, top-3/top-10 coverage, movement). Add `?keyword=<id>` for that keyword's per-point grid |
| GET | `businesses/{id}/proposals/` | Recent proposals and their status |
| POST | `businesses/{id}/proposals/` | Propose a title and/or meta description change |
| GET | `businesses/{id}/pages/` | Every page from the latest completed crawl: URL, HTTP status, title, meta description, canonical, noindex, and that page's findings. Works without WordPress |
| POST | `businesses/{id}/crawl/` | Start a fresh crawl (free). One at a time, at most 3 per website per day |
| GET | `businesses/{id}/search-console/?range=7\|28\|90` | Daily clicks, impressions, CTR, position + comparison with the previous period. Data lags ~2 days |
| GET | `businesses/{id}/search-console/queries/?limit=N` | Stored top queries, up to 5,000 per site (default 500; context/ only gives 20) |
| GET | `businesses/{id}/search-console/pages/?limit=N` | Stored top pages by search performance, up to 5,000 (default 500) |
| POST | `businesses/{id}/search-console/sync/` | Refresh Search Console data now |
| GET | `businesses/{id}/analytics/?range=7\|28\|90` | Google Analytics: organic sessions, active users, conversions (when measurable) per day |
| POST | `businesses/{id}/analytics/sync/` | Refresh Google Analytics data now |
| GET | `businesses/{id}/history/?page=N` | Everything done on this website, newest first, by people and agents. Read it to avoid repeating work |
| GET | `businesses/{id}/connections/` | Each data source: connected?, last refresh, `age_hours`, last error. WordPress also reports `publishing_enabled` and `seo_plugin`. Check before trusting data |
| GET | `businesses/{id}/plan/` | The website's package and what each capability allows: `included`, `conditional`, `optional`, `not_included` |
| GET | `businesses/{id}/benefits/` | Whether hosting and reputation are on (`off`, `on`, `declined`, `revoked`). Read-only |
| GET | `businesses/{id}/checklist/` | All 100 checklist items: eligibility for this package, status, findings, prerequisites, policy notes |
| PUT | `businesses/{id}/checklist/{SEO-xxx}/` | Record a finding or move one item (see rules below) |
| GET | `businesses/{id}/scope-requests/` | Open and past requests for work outside the package (check before asking) |
| POST | `businesses/{id}/scope-requests/` | Ask for work the package doesn't cover. A person decides |
| GET | `businesses/{id}/site-plans/` | The active plan, the open draft (with the reviewer's `decision_note`) and past versions |
| POST | `businesses/{id}/site-plans/` | Start the next draft version (unfinished tasks are carried over) |
| PATCH | `businesses/{id}/site-plans/{plan_id}/` | Edit a draft's `summary` / `reassess_on` |
| POST | `businesses/{id}/site-plans/{plan_id}/tasks/` | Add a task to a draft |
| POST | `businesses/{id}/site-plans/{plan_id}/submit/` | Send the draft for approval |
| POST | `businesses/{id}/site-plans/{plan_id}/withdraw/` | Abandon a draft or submitted plan |
| PATCH | `businesses/{id}/site-plan-tasks/{task_id}/` | Draft: edit the task. Approved plan: record progress only |
| DELETE | `businesses/{id}/site-plan-tasks/{task_id}/` | Remove a task from a draft |

Proposal body:
```json
{
  "target_object_type": "page",
  "target_object_id": 123,
  "proposed_changes": { "title": "…", "meta_description": "…" },
  "reason": "Why this change, in one or two sentences (shown to the approver)."
}
```

`proposed_changes` takes any of these (at least one):

| Field | Value | Fixes crawl finding |
|---|---|---|
| `title` | text, ≤ 200 | Title too long / too short / missing / duplicate |
| `meta_description` | text, ≤ 320 | Description too long / too short / missing / duplicate |
| `canonical_url` | full `https://…` address, ≤ 500 | Missing canonical tag |
| `robots` | `"index"` or `"noindex"` | Page asks not to be indexed |
| `image_alt` | `[{"src": "<image address>", "alt": "<text, ≤ 300>"}]`, up to 20 | Images missing alt text |

Notes:
- `image_alt`: `src` is the image address as it appears on the page (a resized `photo-300x200.jpg` matches `photo.jpg`).
  Alt text is written to the image in the page content and to its media-library item. An image that is in neither
  place makes the change fail with a clear message. Images set by a page builder or theme can't be reached this way.
- `robots`: only propose `"index"` for a page that should rank. Many noindex pages (thank-you pages, archives, author
  pages) are noindex on purpose. Check the finding and the page before proposing, and say why in `reason`.
- Missing H1 headings and other body-content problems can't be changed through the API. Put them in the site plan.
- After publishing, the system reads the page back. If the site ignored a value, the change is marked failed and
  nothing is reported as live. Undo restores the previous values, including image alt text.

## Limits and rules the API enforces

- Title ≤ 200 characters, meta description ≤ 320, reason ≤ 2000. Aim for ~60 and ~155 when writing.
- One open proposal per page (409 until it is decided).
- At most 25 proposals per business per rolling 24 h (`AGENT_MAX_PROPOSALS_PER_BUSINESS_PER_DAY`), 429 beyond that.
- 120 requests/minute per key.
- Only the fields in the table above can be changed. Everything else is out of scope.

## Reading data safely

- Check `connections/` first. If a source isn't connected, say so; never treat "no data" as "zero". `age_hours` tells
  you how old the data is. A recent sync is not proof that a change is live on the site: check the page itself.
- `pages/` is the full URL list. `latest_crawl_status` says if a newer crawl is still queued or running.
- Read `history/` before proposing or starting work, so you don't redo something already done or rejected.
- Local-visibility scans cost money and stay human-only.
- `publishing_enabled: false` on a business means its WordPress is only a content store behind a non-WordPress live
  site. Don't propose title/meta changes there (the API refuses them); put the fix in the site plan instead.
- `seo_plugin` is `aioseo`, `yoast`, `rank_math` or `""`. With `""`, a published title or description won't change on
  the page and the change is marked failed. Say so rather than proposing.

## Package, checklist and scope

Claude and Codex use the same key type and the same endpoints. Each key has a name; that name is recorded on every
change (`agent: claude-daily`), so the history shows which one did what.

**Read the plan first.** Only work on capabilities whose level is `included`. `conditional` needs explicit scope or
evidence in the site plan, `optional` (hosting, reputation) needs the customer's recorded consent, and `not_included`
needs an approved scope change. Never treat a checklist item as in scope just because it is listed.

**Update a checklist item** with `PUT businesses/{id}/checklist/{SEO-xxx}/`:
```json
{ "status": "verified", "finding": "HTTPS is valid and http redirects to https", "evidence": "https://…", "owner": "claude-daily", "next_action": "", "reason": "Initial audit" }
```
Statuses: `not_checked`, `verified` (checked), `needs_work`, `in_progress`, `blocked`, `done`, `n_a`, `out_of_package`.

- `verified`, `needs_work` and `done` need a `finding` (what was observed). "Checked" on its own is not a result.
- `done` also needs `evidence` (proof the work was finished). Done never promises rankings or traffic.
- `blocked` needs `blocker`, `owner` and `next_action`.
- `n_a` needs a `reason`.
- An item the package doesn't include (code `N` or `A`) can only be set to `out_of_package`; then ask with a scope request.
- Items that don't apply to the package (code `X`) can only be `n_a`.
- Sending the same update twice changes nothing, so retrying after a timeout is safe.
- At most 200 updates per website per rolling 24 h (`AGENT_MAX_CHECKLIST_UPDATES_PER_BUSINESS_PER_DAY`), 429 beyond that.
- Refused with 403 if the website's subscription isn't active, 400 if the business is paused.

**Ask for more scope** with `POST businesses/{id}/scope-requests/` and `{"capability": "SEO-060", "note": "why"}`. It only
records the request. Nothing starts and nothing is charged until a person approves it in the app. Errors:
`already_included`, `optional_benefit`, `already_requested` (409), `no_active_subscription`, `unknown_capability`.

**What agent keys can never do:** approve or decline a request, add scope, record customer consent, change a package or
effective date, or edit anything in the subscription. Those routes reject agent keys (401).

## Site plans

A site plan is the agreed work for one website. You draft it; a person approves it in the app. Workflow:

1. `GET site-plans/` — if `open` exists, continue it (read `decision_note`: it's the reviewer's feedback). Otherwise
   `POST site-plans/` with `{"summary": "goals and approach in plain language", "reassess_on": "YYYY-MM-DD"}`.
2. Add tasks, each linked to one checklist item:
   ```json
   { "feature_id": "SEO-026", "title": "Rewrite homepage and service titles", "target": "https://site/…",
     "priority": "high", "risk": "low", "completion_criteria": "New titles live on 5 pages",
     "estimate_minutes": 40, "planned_start": "2026-11-10", "planned_end": "2026-11-20",
     "scope_justification": "only needed for conditional items", "dependencies": "" }
   ```
   Refused with `outside_package` (send a scope request instead), `not_applicable`, `needs_consent` (reputation or
   hosting the customer hasn't turned on), or `justification_required` (conditional items need `scope_justification`
   saying why this site qualifies). At most 50 tasks. Prioritise; never add every checklist item.
3. `POST site-plans/{id}/submit/`. Needs a summary and at least one task. The package is re-checked.
4. A person approves, sends it back with a note (it returns to `draft`), or rejects it. You can't do any of these.
5. Once approved the plan is frozen. Record progress with `PATCH site-plan-tasks/{id}/`:
   `{"status": "in_progress"}`, `{"status": "waiting", "status_note": "Waiting for CMS login"}`,
   `{"status": "done", "completion_evidence": "what changed and where it can be seen"}`, or `deferred` / `cancelled`
   with a `status_note`. Starting a task is refused if the package no longer covers it (for example after a downgrade).
   To change the plan itself, start a new draft version.

`estimate_minutes`, `risk`, `scope_justification` and `completion_evidence` are internal: customers never see them.
At most 200 plan changes per website per rolling 24 h (`AGENT_MAX_PLAN_WRITES_PER_BUSINESS_PER_DAY`).

## Example

```bash
KEY=sp_agent_...
BASE=https://seopilot.theservicepilot.com/api/v1/agent
curl -s -H "Authorization: Agent $KEY" $BASE/businesses/
curl -s -H "Authorization: Agent $KEY" $BASE/businesses/1/context/
curl -s -H "Authorization: Agent $KEY" "$BASE/businesses/1/wordpress/pages/?type=page"
curl -s -X POST -H "Authorization: Agent $KEY" -H "Content-Type: application/json" \
  -d '{"target_object_type":"page","target_object_id":123,"proposed_changes":{"meta_description":"…"},"reason":"Missing description flagged by the crawl"}' \
  $BASE/businesses/1/proposals/
```

## Suggested standing instructions for the agent

> For each business: read `context/`. For each issue about a title or meta description,
> find the page in `wordpress/pages/` (match by URL), write one improved title and/or
> description grounded only in what the page and site already say, and POST it with a
> one-sentence reason. Never invent claims, prices, locations, or guarantees. Skip pages
> that already have an open proposal. Do not retry a rejected proposal for the same page
> unless the issue changed. Stop at the daily limit. Report what you proposed.
