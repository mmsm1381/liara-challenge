# Liara API notes

Nothing here has been checked against a live account yet (the build container cannot reach
`api.liara.ir`). Run `python manage.py probe_liara --token ... --app <throwaway> --write --notes API_NOTES.md`
and update the status column.

Sources: `paas.yaml`, `spec_dbaas.yaml`, `spec_object-storage.yaml` (liara-cloud/openapi) and the CLI's
`src/commands/deploy.ts` (liara-cloud/cli). All calls send `Authorization: Bearer <token>` and, for teams, `?teamID=`.

## Endpoints used

| Client method | Method and path | Source | Status |
|---|---|---|---|
| list_projects | GET `/v1/projects` | paas.yaml | from spec, not live-verified |
| get_project | GET `/v1/projects/{name}` | paas.yaml | from spec, not live-verified |
| create_project | POST `/v1/projects` `{name, planID, platform, network?}` | paas.yaml | from spec, not live-verified |
| get_logs | GET `/v1/projects/{name}/logs?since=` | paas.yaml | from spec, not live-verified (unit of `since` unknown) |
| list_releases | GET `/v1/projects/{name}/releases?page=&count=` | paas.yaml | from spec, not live-verified |
| get_metrics_summary | GET `/v1/projects/{name}/metrics/summary` | paas.yaml | from spec, not live-verified |
| restart | POST `/v1/projects/{name}/actions/restart` | paas.yaml | from spec, not live-verified |
| resize | POST `/v1/projects/{name}/resize` `{planID}` | paas.yaml | from spec, not live-verified |
| update_envs | POST `/v1/projects/update-envs` `{project, variables:[{key,value}]}` | paas.yaml | from spec, not live-verified |
| upload_source | POST `/v2/projects/{name}/sources` multipart `file` -> `{sourceID}` | paas.yaml + deploy.ts | from spec, not live-verified |
| create_release | POST `/v2/projects/{name}/releases` -> `{releaseID}` | paas.yaml + deploy.ts | from spec, not live-verified (extra fields `build`, `message`, `healthCheck`, `platformConfig` from the CLI only) |
| get_build_logs | GET `/v2/releases/{id}/build-logs?since=` | CLI only | UNVERIFIED guess (response shape and `since` unit assumed) |
| cancel_build | POST `/v2/releases/{id}/cancel` | CLI only | UNVERIFIED guess |
| rollback | create_release reusing an old release's `sourceID` | none | UNVERIFIED guess, see below |
| list_plans | GET `/v1/plans` | none | UNVERIFIED guess, no plans endpoint in any spec |
| list_databases | GET `/v1/databases` | spec_dbaas.yaml | from spec, not live-verified |
| create_database | POST `/v1/databases` `{type, hostname, planID, version?, publicNetwork}` -> `{databaseID}` | spec_dbaas.yaml | from spec, not live-verified |
| get_database | GET `/v1/databases/{id}` | spec_dbaas.yaml | from spec, not live-verified |
| list_buckets | GET `{storage}/api/v1/buckets` | spec_object-storage.yaml | from spec, not live-verified |
| create_bucket | POST `{storage}/api/v1/buckets` `{name, plan, permission}` | spec_object-storage.yaml | from spec, not live-verified (plans `20g/40g/80g/160g`) |
| create_bucket_key | GET `{storage}/api/v1/buckets/{name}` then POST `{storage}/api/v1/keys` | spec_object-storage.yaml | partly UNVERIFIED: the request `buckets` item shape and the create response (secret key) are not in the spec |
| account / balance | none | none | omitted, no endpoint in any spec; a frozen app shows up as `400 frozen_project` |

## Open questions

| Question | What the spec says | Still unknown |
|---|---|---|
| Base URL: `api.iran.liara.ir` vs `api.liara.ir` | All three specs say `https://api.liara.ir` (storage: `https://storage-service.liara.ir`). The CLI picks a host per region and uses `.iran.liara.run` app domains for the iran region without a private network. | Whether one token works on both hosts, and which host an account created in a given region needs. Default in settings is `api.iran.liara.ir`; override with `LIARA_API_BASE`. App URL in `get_project` is derived from this. |
| Plan list and prices | No plans endpoint. Plans appear only as `planID` strings. | The real endpoint (if any), plan IDs, RAM/CPU/disk, price unit (toman, per hour or month). `list_plans` is a guess; `sizing.py` may need a hard-coded table until confirmed. |
| Metrics period | `metrics/summary` returns `cpuUsage`, `memoryUsage` (lists of `{applet, value:[number or string]}`) and `disksUsage` (`{name, reportedAt, size, usage}`). | Time window of the samples, the unit of memory (bytes?), whether CPU is a percent or cores, the layout of `value`. The client returns the raw numbers; the diagnostics must not assume units. |
| Log `since` | Query parameter `since`, "show logs since timestamp", type string. | Unix seconds, milliseconds or ISO. The client sends unix seconds. |
| Database internal host | `DB-details` has `hostname`, `port`, `internalPort`, `node.host`, `publicNetwork`, `root_password`, `username`, `dbName`. | Which value an app on Liara's private network should use. The client returns `host` = node host or hostname, `internal_host` = `hostname`, `internal_port`. Verify with a real deploy. |
| Does an env update restart the app? | `update-envs` returns `{message}`; nothing about restarts. | Whether a restart or a new release is needed. `jobs.py` should call `restart` after `update_envs` until proven unnecessary. Also unknown: whether `variables` replaces all envs; `update_envs` re-sends existing envs by default (merge) to be safe. |
| Rollback | No rollback endpoint. Releases have `sourceAvailable` (bool) but no source id. | Whether a release's id can be passed as `sourceID`, or whether the API exposes a source id. Until then `rollback` raises `bad_request` with a Persian message when `list_releases` shows no `source_id`. |
| Release state names | CLI reads `DEPLOYING`, `READY`, `FAILED`, `TIMEOUT`, `CANCELED`, `UNHEALTHY`, `PUSHED`. | Full list and which are terminal. |
| Build-log response | CLI: `release.state`, `release.failReason`, `buildOutput` lines, `since`. | Exact shape of each `buildOutput` item and the right `next_since`. The client accepts strings or `{line|message, timestamp}`. |
| Are tokens JWTs? | Security schemes are `jwt` (apiKey header `Authorization`) and `bearerAuth` (HTTP bearer, format JWT). | Whether API keys from the console are JWTs and how long they live. `redact` masks JWT-shaped strings in any case. |
| Source size limit | The CLI rejects archives above `MAX_SOURCE_SIZE`; server returns 413. | The exact limit per plan. `build_archive` enforces 200 MB. |
| Error body format | CLI reads `message` (for example `frozen_project`) and `data.code` on 428 (`max_deployment_count_in_day`, ...). | Other codes. The client maps 401/403, 404, 400 `frozen_project`, 429, 5xx; everything else becomes `bad_request` with the redacted `message`. |
