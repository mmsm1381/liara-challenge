# Liara Assistant — build plan and status

Source of truth: *Liara Assistant — Implementation Plan* (Oct 23, 2026) and the
three product screens. This file tracks how we build it and what is done.

## Starting point

The repository started empty. The previous agent's `liara-compress.zip` and
`liara_deploy.py` were not available, so the modules the plan says to reuse
(`inspection`, `model`, `liara` + `redact`, `diagnostics`, the demo app, the
test-ZIP builder, `probe_liara`) are rewritten here to the same contract.

Liara endpoints come from the public OpenAPI specs (`liara-cloud/openapi`:
`paas.yaml`, `dbaas.yaml`, `object-storage.yaml`) and the CLI
(`liara-cloud/cli` `src/commands/deploy.ts`). The build container cannot
reach `api.liara.ir`, so nothing has been verified live yet; see
`API_NOTES.md`.

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
  inspection.py         safe static ZIP inspection + service detection
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
| M0 | Clean base: Django + Postgres + Redis + worker in Docker Compose, empty chat page | in progress |
| M1 | `liara_client.py`, `probe_liara`, `API_NOTES.md` | spec-only; live probe needs a token |
| M2 | Chat shell + Phase 1 (connect account) | todo |
| M3 | Inspection + recommendation up to the approval card | todo |
| M4 | Deploy job with progress and verification | todo |
| M5 | Returning user, diagnosis and fixes | todo |
| M6 | Polish, README, demo script | todo |

## Who builds what

Planning, the data model, the approval gate, the orchestrator and the deploy
job are done by the main agent. Well-specified, self-contained pieces go to
cheaper models:

- **Sonnet:** `inspection.py`, `liara_client.py` from the spec, chat UI
  (templates/CSS/JS), `diagnostics.py`.
- **Haiku:** demo Django app, `create_test_projects.py`, Docker files.

## Live checks still owed (need a real Liara account)

Everything in "Open questions to settle in M1" of the plan, plus the M4/M5
end-to-end checks and the demo video. Run
`python manage.py probe_liara --token ... --app <throwaway>` and update
`API_NOTES.md`.
