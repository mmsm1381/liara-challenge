# Liara Assistant

A chat-only assistant, written in Persian, for people who run sites on Liara. You bring your own Liara API key, and the assistant deploys a project (ZIP upload) of any Liara platform (Django, Flask, Laravel, Node.js, Next.js, static, Go, Docker, …), detected by AI and confirmed by the user, together with its database and file storage. Later, you can ask it about the sites it deployed: it answers from its saved history and from fresh read-only data. Build status and open work are tracked in `PLAN.md`.

> **Status:** nothing has been verified against a live Liara account yet. Read [Limitations](#4-limitations) before relying on any Liara call.

---

## 1. What it is

- A Persian (RTL) chat UI. The assistant only talks in chat; there is no dashboard for it.
- The user connects a Liara API key, which is stored encrypted. The assistant then creates the app, database and bucket, sets the environment variables, uploads the code, and checks the site.
- For every site it deploys, it keeps a record: plan, answers given, a deploy-day baseline (RAM, CPU, response time) and short notes. Later questions ("چرا سایتم کند شده؟") use this history plus fresh read-only Liara data.
- It never changes anything on its own. Every change becomes an approval card that the user must confirm.

## 2. Architecture

```
 Browser (RTL chat, vanilla JS)
   | JSON: /chat/send  /chat/key  /chat/upload  /chat/approve  /chat/reset
   | SSE:  /chat/stream?job=<id>                      (progress of a running job)
   v
 Django chat API  (assistant/views.py)
   |
   +-- flows.py         scripted steps: connect, deploy, returning user
   |
   +-- orchestrator.py  LLM tool loop: max 8 tool calls, 60 s per message
   |        |  (llm.py -> Liara AI, OpenAI-compatible; optional)
   |        v
   |   tools.py registry
   |     read tools   -> run now (Liara reads, HTTP check of the site, diagnosis)
   |     change tools -> PendingAction + approval card (approvals.py)
   |                          | user taps Confirm (15-minute expiry, atomic claim)
   v                          v
 (no LLM configured, cap reached, or model error ->
  deterministic rule answer in orchestrator.py)     RQ worker (jobs.py)
                                                      runs the action once,
                                                      polls the build, verifies,
                                                      saves Site / Release records
                                                             |
                                                             v
                                                    Liara API (liara_client.py)
                                                    PaaS, DBaaS, object storage

 PostgreSQL: app data        Redis: RQ queue        (RQ_SYNC=1: jobs run in a thread)
```

**Where the LLM fits.** The LLM only reads tool results and proposes changes through `propose_*` tools. It cannot call Liara directly and cannot execute anything. If no LLM is configured (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` not all set), the daily cap is reached, or the model fails, `orchestrator.py` answers with deterministic rules instead: a site-status check, a diagnosis from the signal rules in `diagnostics.py`, or a menu of buttons.

**Upload path.** `inspection.py` only checks that the ZIP is safe to read: paths, links and special files, encryption, size and zip-bomb limits, CRC, and secret-looking files such as `.env`, which it flags. It does not decide what the project is. `ai_detect.py` does that: one LLM call on a redacted slice of the archive, then strict validation of the JSON answer (see Phase 2). Without a configured LLM, uploads are refused.

**Data models** (`assistant/models.py`):

| Model | What it holds |
|---|---|
| `LiaraConnection` | One per user. Fernet-encrypted API token, optional team ID, expiry from the JWT `exp` claim, status `active` or `invalid`. |
| `Site` | One per app the assistant knows about: URL, platform, plan, DB and bucket names, answers, deploy-day `baseline`, status (`deploying`, `live`, `failed`, `unknown`, `gone`). |
| `Release` | Release IDs per site with source ID, state and fail reason. |
| `SiteNote` | Short timeline notes per site (`deploy`, `incident`, `fix`, `decision`). These are the assistant's memory. |
| `Conversation` | One thread per user. `state` holds the position in the scripted flow. |
| `Message` | Chat messages. The UI blocks (buttons, cards, approval, progress, ...) are stored in `ui`. |
| `PendingAction` | A proposed change: tool, args, SHA-256 digest of the args, card lines, status, 15-minute expiry. |
| `Job` | The worker run for one approved action: steps, current step, result, error, timestamps. |
| `AuditLog` | A redacted record of each Liara change call and its result. |
| `LLMUsage` | Per-user daily count of LLM calls, checked against `LLM_DAILY_CAP`. |

## 3. How it connects to the Liara API

**Auth.** Every call sends `Authorization: Bearer <token>`. If a team ID is stored, `teamID` is added as a query parameter. The connect flow stores no team ID, so the assistant works with the account's default team only.

**Token handling.**
- The token is encrypted with Fernet. The key comes from `TOKEN_ENCRYPTION_KEY`, which is required outside `DEBUG`.
- The key is submitted through `POST /chat/key`. Only the encrypted token is stored, and only after Liara accepts it. The chat shows "🔑 کلید API فرستاده شد" instead of the key.
- The token is not logged and is not sent to the LLM. `LiaraClient.__repr__` hides it, and error bodies have the token replaced before they are used.
- The UI says: "کلید رمزنگاری‌شده ذخیره می‌شود و هیچ‌وقت برای مدل هوش مصنوعی فرستاده نمی‌شود."
- A key pasted into the normal message box (instead of the password field) is routed to the same check and is never saved as a chat message.

**Client.** `assistant/liara_client.py` is the only module that talks to Liara. It has typed methods only; there is no "call any URL" method. Change calls (POST) are never retried.

**Endpoints** (copied from `API_NOTES.md`):

| Client method | Method and path | Source | Status |
|---|---|---|---|
| `list_projects` | GET `/v1/projects` | paas.yaml | from spec, not live-verified |
| `get_project` | GET `/v1/projects/{name}` | paas.yaml | from spec, not live-verified |
| `create_project` | POST `/v1/projects` `{name, planID, platform, network?}` | paas.yaml | from spec, not live-verified |
| `get_logs` | GET `/v1/projects/{name}/logs?since=` | paas.yaml | from spec, not live-verified (unit of `since` unknown) |
| `list_releases` | GET `/v1/projects/{name}/releases?page=&count=` | paas.yaml | from spec, not live-verified |
| `get_metrics_summary` | GET `/v1/projects/{name}/metrics/summary` | paas.yaml | from spec, not live-verified |
| `restart` | POST `/v1/projects/{name}/actions/restart` | paas.yaml | from spec, not live-verified |
| `resize` | POST `/v1/projects/{name}/resize` `{planID}` | paas.yaml | from spec, not live-verified |
| `update_envs` | POST `/v1/projects/update-envs` `{project, variables:[{key,value}]}` | paas.yaml | from spec, not live-verified |
| `upload_source` | POST `/v2/projects/{name}/sources` (multipart `file`) -> `sourceID` | paas.yaml + deploy.ts | from spec, not live-verified |
| `create_release` | POST `/v2/projects/{name}/releases` -> `releaseID` | paas.yaml + deploy.ts | from spec, not live-verified (`build`, `message`, `healthCheck`, `platformConfig` come from the CLI only) |
| `get_build_logs` | GET `/v2/releases/{id}/build-logs?since=` | CLI only | **UNVERIFIED** guess (response shape and `since` unit assumed) |
| `cancel_build` | POST `/v2/releases/{id}/cancel` | CLI only | **UNVERIFIED** guess (not used by any flow) |
| `rollback` | create_release reusing an old release's `sourceID` | none | **UNVERIFIED** guess; raises `bad_request` when no `source_id` is exposed (the usual case) |
| `list_plans` | GET `/v1/plans` | none | **UNVERIFIED** guess, no plans endpoint in any spec |
| `list_databases` | GET `/v1/databases` | spec_dbaas.yaml | from spec, not live-verified |
| `create_database` | POST `/v1/databases` `{type, hostname, planID, version?, publicNetwork}` -> `databaseID` | spec_dbaas.yaml | from spec, not live-verified |
| `get_database` | GET `/v1/databases/{id}` | spec_dbaas.yaml | from spec, not live-verified |
| `list_buckets` | GET `{storage}/api/v1/buckets` | spec_object-storage.yaml | from spec, not live-verified |
| `create_bucket` | POST `{storage}/api/v1/buckets` `{name, plan, permission}` | spec_object-storage.yaml | from spec, not live-verified (plans `20g/40g/80g/160g`) |
| `create_bucket_key` | GET `{storage}/api/v1/buckets/{name}`, then POST `{storage}/api/v1/keys` | spec_object-storage.yaml | **partly UNVERIFIED**: the `buckets` item shape and the secret key in the response are not in the spec |
| account / balance | none | none | not implemented; a frozen app shows up as `400 frozen_project` |

**Error mapping** (`_raise_for_response` and `_DEFAULT_FA`). Each `LiaraError` carries a code and a Persian message that is safe to show:

| Code | When | Persian message shown to the user |
|---|---|---|
| `unauthorized` | 401, 403 | این کلید معتبر نیست یا منقضی شده. |
| `not_found` | 404 | این مورد در حساب لیارا پیدا نشد. |
| `frozen` | 400 containing `frozen_project` | موجودی حساب لیارا کافی نیست؛ حساب را در کنسول لیارا شارژ کنید. |
| `rate_limited` | 429 | تعداد درخواست‌ها به لیارا زیاد بود؛ چند لحظه بعد دوباره امتحان کنید. |
| `unavailable` | 5xx, connection errors | لیارا الان پاسخ نمی‌دهد؛ یک دقیقه دیگر دوباره امتحان کنید. |
| `timeout` | request timed out | لیارا دیر جواب داد؛ یک دقیقه دیگر دوباره امتحان کنید. |
| `bad_request` | other 4xx (409 has its own text) | لیارا این درخواست را نپذیرفت. پیام لیارا: ... (redacted, 200 chars max) |
| `unknown` | redirects, unexpected responses | پاسخ غیرمنتظره‌ای از لیارا آمد. |

**Changes and timeouts.** `jobs.change()` wraps every Liara change call:
- The call is audited and never retried.
- A timeout does not count as failure. The action is marked `unknown`, the worker reads the app's status back from Liara (`get_project`), and the chat says that Liara did not answer in time, what state it read, and to check before repeating.
- Other errors fail the step, and the chat shows the Persian message.

Read calls are different: while a build is running, the worker keeps polling `get_build_logs` on `timeout` or `unavailable`.

**Redaction.** `assistant/redact.py` masks Bearer tokens, JWT-shaped strings, credentials inside URLs, `KEY=value` pairs for secret-looking names, and long opaque strings. `redact_obj` is applied to every tool result before the LLM sees it, to build logs and evidence, and to audit records. Audit logs store only the names of env vars, never their values.

## 4. Limitations

- **An LLM must be configured to deploy anything.** Without `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`, uploads are refused, including in `LIARA_FAKE` demo mode. Detection quality depends on the model, and it has not been tested with a real Liara AI model. Each upload uses one LLM call from the daily cap (`LLM_DAILY_CAP`).
- **Nothing is verified against a live Liara account.** The build container cannot reach `api.liara.ir`. `API_NOTES.md` lists the open questions, which include:
  - The base URL: the default is `https://api.iran.liara.ir` and the specs say `https://api.liara.ir`. Override with `LIARA_API_BASE`.
  - The plan list. `/v1/plans` is a guess and no spec defines it. Plan IDs, RAM/CPU and prices are unknown, and the sizing recommendation depends on them.
  - Rollback. There is no rollback endpoint, so the rollback tool fails with a Persian message unless release source IDs are exposed.
  - Build logs: the response shape and the unit of `since`.
  - Metrics: the time window, the memory unit and the CPU unit.
  - Whether an env update restarts the app. The worker calls `restart` after `update_envs`.
  - Release state names and which are terminal.
  - The token format.
- **Object storage env vars are guesses.** `LIARA_ENDPOINT` is hard-coded and marked UNVERIFIED in `jobs.py`. The bucket-key response is also unverified; if Liara does not return the secret, the job stops and asks the user to create the key in the console.
- **Shipping a new version of an existing site is not supported in place.** The "نسخه‌ی جدید بفرستم" button starts the deploy flow again, which creates a new app with a random suffix.
- **Port mismatch is advice only.** The `port_mismatch` signal tells the user which port to set and to send a new version; it does not redeploy by itself.
- **GitHub sources are not supported.** The assistant accepts ZIP uploads only (50 MiB by default, `MAX_ZIP_BYTES`; 200 MB after packing). Project detection is AI-only (see Phase 2), so every upload needs an LLM configured.
- **Custom domains: guidance only.** The "دامنه‌ی خودم را وصل کن" button shows steps and links to the console. It does not add domains or DNS records.
- **Redis is not created automatically.** Provision it on Liara and set `REDIS_URL`. A worker process is also needed unless `RQ_SYNC=1` (demo only).
- **No deletes.** The assistant never deletes apps, databases or buckets. Cleanup is a message asking the user to delete them in the console.
- **Single Liara team.** One connection per user, with no team selection in the flow.
- **Some env vars must be set in the Liara console.** `propose_env_change` can only generate `SECRET_KEY`, `DJANGO_SECRET_KEY`, `ALLOWED_HOSTS` and `DJANGO_ALLOWED_HOSTS`. Any other variable (third-party API keys, for example) gets a link to the console.
- **Metrics history is thin.** The baseline is one snapshot taken at deploy time. Trends come only from what `metrics/summary` returns for the current window.
- **LLM answers are not evaluated.** `docs/safety-cases.md` lists acceptance ideas, not results of runs. The test suite covers flows, inspection, the client, the LLM transport and the orchestrator, but not a live Liara account.

## 5. How the user interacts

Chat is the only interface. The page shows the conversation with buttons, cards and approval prompts.

**Phase 1: connect the account.** The assistant greets the user and offers "کلید API دارم" or "نمی‌دونم چطور بگیرم". The key is pasted into a password-mode box. It is checked with a list-projects call, and only then stored encrypted. The assistant lists the apps it found. If the key is a JWT, its expiry is saved, and the returning-user greeting warns when fewer than three days remain. Screenshot: `docs/screens/1-connect.png`.

**Phase 2: deploy.** Flow:
1. The user attaches a ZIP. Inspection is static only: nothing is extracted to disk or run. The card shows the detected platform, start command, port and services (PostgreSQL, MySQL, MongoDB, Redis, file storage), with the reason for each.
   - **AI-only detection.** Every upload is classified by `ai_detect.py`. The model gets a small, redacted slice of the ZIP: the file tree, manifests such as `requirements.txt`, `composer.json` and `go.mod`, the first lines of entry points, and env var names only from `.env.example` (never `.env`). It answers with one JSON object, and the server validates every field:
     - the platform must be one of Liara's 14 platforms (the CLI's `AVAILABLE_PLATFORMS`);
     - Liara's fixed ports are enforced (for example 80 for Django, Flask and Laravel);
     - the start command is display-only and must be a plain launcher command;
     - every service and env var name must appear verbatim in the files that were sent;
     - a `platform` declared in `liara.json` always wins;
     - a `blocker_fa` answer (the project cannot run as uploaded) stops the deploy and shows that Persian reason;
     - low confidence, or any field that fails validation, means the project is not deployed.

     Every upload is confirmed in chat with «این تشخیص درست است؟» ("is this right?") before the visitor and purpose questions. File contents are treated as untrusted data.
2. Two questions: daily visitors ("کمتر از ۱۰۰", "۱۰۰ تا ۱۰۰۰", "بیشتر از ۱۰۰۰", "نمی‌دانم") and purpose ("کسب‌وکار واقعی" or "تست یا شخصی").
3. A recommendation built from the live plan list, with the price and a reason. The user can ask for "ارزان‌ترش کن".
4. An approval card with cost and downtime. "قبول، همین را بساز" starts the job. The job wires the database env vars (including Laravel's names), generates secrets such as `SECRET_KEY` and `APP_KEY` for Laravel, and sets `ALLOWED_HOSTS` and `APP_URL`.
5. Live progress over SSE, step by step, followed by the site URL `https://<app>.liara.run` and a note that the home page answered.

Screenshots: `docs/screens/2-deploy-approval.png` (approval card) and `docs/screens/2-deploy-done.png` (finished deploy). Both were taken with the fake Liara client, so the plan names and prices in them are invented.

**Phase 3: returning user.** The assistant reloads the user's apps from Liara and shows a card of sites with quick replies: "سایت‌هایم چطورند؟" (checks each URL), "چرا X کند شده؟" (diagnosis) and "نسخه‌ی جدید بفرستم". A diagnosis calls `diagnose_site`, which collects fresh status, logs, metrics, releases and an HTTP check. It compares them with the baseline in a "روز دیپلوی در برابر الان" table, runs the signal rules, and names the most likely cause with its evidence. Signals and their fixes:

| Signal | Likely cause | Proposed fix |
|---|---|---|
| `frozen` | Account balance too low | none (charge the account) |
| `missing_dependency` | Missing package in requirements or package.json | roll back to the previous release |
| `missing_env` | Env var not set | set it (auto only for the four generated names) |
| `database` | Cannot reach the database | restart the app |
| `memory` | Plan too small for the traffic | plan one size up |
| `port_mismatch` | Listening port differs from the release port | advice only: set the port and send a new version |
| `bad_release` | Crash started right after the last release | roll back to the previous release |

Screenshot: `docs/screens/3-returning.png` (site list, quick replies, the comparison table, the fix proposal, the approval card and the verification result).

**Approvals.** A change is never made without a tap on "تأیید".
- Each proposal expires after 15 minutes. An expired card cannot be confirmed; the user has to ask again.
- The args are stored with a SHA-256 digest. The worker refuses to run if they changed, so the approved action is exactly the one shown.
- Confirmation claims the action atomically (`select_for_update` on decide, a conditional update on claim), so it runs once.
- Double taps and stale cards show the real state and do nothing.
- Rejecting a deploy builds and charges nothing. The assistant says so.

## 6. Running it

**Docker Compose (local).**

```bash
cp .env.example .env
# Set TOKEN_ENCRYPTION_KEY in .env to a generated key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
docker compose up --build
```

Open http://localhost:8000, sign up, and start the chat. Replace the placeholder values in `.env`. Compose starts `web` (runs `migrate`, then gunicorn), `worker` (`rqworker default`), PostgreSQL 16 and Redis 7. Compose sets `DJANGO_DEBUG=1` for local runs.

**Demo mode, no Liara account (`LIARA_FAKE=1`).** The fake client keeps its state in `var/fake_liara.json`, shared by web and worker. It starts with two apps, `my-shop` (node) and `blog` (static). Any API key is accepted except one that starts with `bad` (or is exactly `fake-invalid`), which is rejected as unauthorized.

```bash
LIARA_FAKE=1 docker compose up --build        # or set LIARA_FAKE=1 in .env
```

Deploys in demo mode still need the LLM variables (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`) set in `.env`, because project detection is AI-only. The same applies to the local run below. Diagnosis works without them through the rule-based fallback.

To break an app for a diagnosis demo, set one of its scenarios: `missing_package`, `oom`, `missing_env` or `healthy`.

```bash
docker compose exec web python manage.py shell -c "from assistant.liara_client import fake_set_scenario; fake_set_scenario('my-shop', 'missing_package')"
```

**Local without Docker.** Uses SQLite (`db.sqlite3`) and runs jobs inline.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python manage.py migrate
RQ_SYNC=1 LIARA_FAKE=1 python manage.py runserver
```

With `DJANGO_DEBUG` unset, settings default to debug mode, which derives a dev-only token key from `SECRET_KEY`. `TOKEN_ENCRYPTION_KEY` is not needed for that.

**Tests.**

```bash
python -m pytest
```

**Checking endpoints against a real account.** Use a throwaway app. Reads are safe; `--write` also updates one env var (`LIARA_ASSISTANT_PROBE=1`) and restarts that app.

```bash
python manage.py probe_liara --token "$LIARA_TOKEN" --app <throwaway-app>
python manage.py probe_liara --token "$LIARA_TOKEN" --app <throwaway-app> --write --notes API_NOTES.md
```

Flags, from `assistant/management/commands/probe_liara.py`: `--token` (required), `--team` (optional team ID), `--app` (required, the only app writes touch), `--write` (also call the change endpoints), `--notes FILE` (append the result table to a file). The probe prints only HTTP status and top-level response keys, never values. Its "verified" label only means the call returned without error; update the status column in `API_NOTES.md` by hand after reading the result.

**Deploying the assistant on Liara.** See [`docs/DEPLOY_ASSISTANT.md`](docs/DEPLOY_ASSISTANT.md). In short: a Docker app on port 8000 built from the root `Dockerfile`, a PostgreSQL database, a Redis instance, a second app from the same image running `python manage.py rqworker default`, and the environment variables listed there (`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `REDIS_URL`, `TOKEN_ENCRYPTION_KEY`, `LLM_*`). Do not use `RQ_SYNC=1` outside a demo.

## 7. Project layout

```
config/                  Django settings, urls, wsgi
assistant/
  models.py              data models (section 2)
  crypto.py              Fernet encryption of tokens
  redact.py              strips tokens, passwords and URLs from text
  liara_client.py        the only module that talks to Liara; fake client for demos
  llm.py                 Liara AI transport (native tools or prompt_json)
  inspection.py          ZIP safety checks
  ai_detect.py           AI project detection with strict validation
  sizing.py              turns the answers and plan list into a package and price
  diagnostics.py         deterministic signal rules
  tools.py               tool registry: read tools run, change tools propose
  orchestrator.py        LLM tool loop and rule-based fallback
  flows.py               scripted chat flows (connect, deploy, returning user)
  approvals.py           approval gate: propose, decide, claim, expiry
  jobs.py                RQ jobs: deploy, rollback, restart, plan change, env change
  views.py, urls.py      chat page, JSON endpoints, SSE stream
  templates/, static/    RTL chat UI (vanilla JS)
  management/commands/   probe_liara
docs/                    CONTRACTS.md (UI blocks, endpoints), DEPLOY_ASSISTANT.md,
                         safety-cases.md, screens/
examples/django-postgresql-demo/   sample Django app for trying a deploy
tools/create_test_projects.py      generates credential-free test ZIPs
tests/                   pytest suite
API_NOTES.md             Liara endpoints and their verification status
PLAN.md                  build plan and milestones
```
