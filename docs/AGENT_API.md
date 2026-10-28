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
| GET | `businesses/` | All customer businesses (id, name, website, customer, `wordpress_connected`) |
| GET | `businesses/{id}/context/` | Site-health findings, open issues, top queries/pages, open proposals |
| GET | `businesses/{id}/wordpress/pages/?type=page\|post` | WordPress pages/posts (ids needed to propose) |
| GET | `businesses/{id}/local-visibility/` | Google Maps rank per tracked keyword (visibility score, average rank, top-3/top-10 coverage, movement). Add `?keyword=<id>` for that keyword's per-point grid |
| GET | `businesses/{id}/proposals/` | Recent proposals and their status |
| POST | `businesses/{id}/proposals/` | Propose a title and/or meta description change |

Proposal body:
```json
{
  "target_object_type": "page",
  "target_object_id": 123,
  "proposed_changes": { "title": "…", "meta_description": "…" },
  "reason": "Why this change, in one or two sentences (shown to the approver)."
}
```

## Limits and rules the API enforces

- Title ≤ 200 characters, meta description ≤ 320, reason ≤ 2000. Aim for ~60 and ~155 when writing.
- One open proposal per page (409 until it is decided).
- At most 25 proposals per business per rolling 24 h (`AGENT_MAX_PROPOSALS_PER_BUSINESS_PER_DAY`), 429 beyond that.
- 120 requests/minute per key.
- Only titles and meta descriptions can be changed. Everything else is out of scope.

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
