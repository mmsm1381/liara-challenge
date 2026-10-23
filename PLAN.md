# Liara Assistant — build plan and status

Source of truth: *Liara Assistant — Implementation Plan* (Oct 23, 2026) and the
three product screens. This file tracks how we build it and what is done.

## Starting point

Started from an empty repository plus the previous agent's `liara-compress.zip`.
Reused from it: `inspection.py` (now the ZIP safety checks only; project
detection moved to `ai_detect.py`),
the Django + PostgreSQL demo app, `create_test_projects.py` and the safety
cases (`docs/safety-cases.md`). The LLM transport and `redact()` were ported
into `llm.py` / `redact.py`. Everything else from the zip (form pages, fixture
executor, host bindings, plan contracts) was left out as the plan says.
`liara_deploy.py` was not provided; the deploy job follows the CLI's
`deploy.ts` instead.

Liara endpoints come from the public OpenAPI specs (`liara-cloud/openapi`:
`paas.yaml`, `dbaas.yaml`, `object-storage.yaml`) and the CLI. The build
container cannot reach `api.liara.ir`, so nothing has been verified live yet;
see `API_NOTES.md`.

## Layout

```
config/                 Django settings, urls, wsgi
assistant/
  models.py             LiaraConnection, Site, Release, SiteNote, Conversation,
                        Message, PendingAction, Job, AuditLog
  crypto.py             Fernet token encryption
  redact.py             strips tokens, passwords, URLs from text
  liara_client.py       the only module that talks to Liara (typed functions)
  llm.py                Liara AI chat-completions transport (native tools + prompt_json)
  inspection.py         ZIP safety checks (no project detection)
  ai_detect.py          AI project detection with strict validation
  github.py             public GitHub repo link -> ZIP, same path as an upload
  sizing.py             plan tiers -> real plan IDs, price text
  diagnostics.py        deterministic log/metric signal rules
  tools.py              tool registry; read tools run, change tools -> PendingAction
  orchestrator.py       LLM tool loop (8 calls / 60 s)
  flows.py              scripted chat flows: connect, deploy, returning user
  approvals.py          approve/reject, atomic claim, 15-min expiry
  jobs.py               RQ jobs: deploy, rollback, restart, plan change, env change
  views.py              chat page, /chat/send, /chat/upload, /chat/approve, /chat/stream (SSE)
  templates/, static/   one-column RTL chat UI, vanilla JS
  management/commands/probe_liara.py
examples/django-postgresql-demo/
tools/create_test_projects.py
tests/
```

## Milestones

| # | Milestone | Status |
|---|-----------|--------|
| M0 | Clean base: Django + Postgres + Redis + worker in Docker Compose, chat page | done locally; not yet deployed on Liara |
| M1 | `liara_client.py`, `probe_liara`, `API_NOTES.md` | done from spec; **live probe still owed** |
| M2 | Chat shell + Phase 1 (connect account) | done, tested against the fake client |
| M3 | ZIP inspection is safety-only; project detection is AI (validated, user-confirmed); recommendation up to the approval card | done; prices come from `list_plans()` (endpoint unverified) |
| M4 | Deploy job with progress (SSE) and verification | done against the fake client |
| M5 | Returning user, diagnosis and fixes (rollback, plan change, env var) | done against the fake client; LLM path tested with a mocked model |
| M6 | README, screenshots | README + screenshots from the fake client; demo video owed |

Test suite: `python -m pytest` (flows, approval gate, inspection, client, LLM loop and prompt-injection cases).

## Who builds what

Planning, the data model, the approval gate, the orchestrator and the deploy
job are done by the main agent. Well-specified, self-contained pieces go to
cheaper models:

- **Sonnet:** `liara_client.py` (+ fake client, `probe_liara`, `API_NOTES.md`),
  inspection extension, chat UI (templates/CSS/JS), LLM-path tests.
- **Haiku:** Docker/Compose files, demo-app `liara.json`, README.

## Live checks still owed (need a real Liara account)

Everything in "Open questions to settle in M1" of the plan, plus the M4/M5
end-to-end checks and the demo video. Run
`python manage.py probe_liara --token ... --app <throwaway>` and update
`API_NOTES.md`.
