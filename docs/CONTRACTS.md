# Internal contracts

Shared interfaces between modules. Change them here first.

## Message UI blocks (`Message.ui`, a list)

| type | fields | renders as |
|------|--------|-----------|
| `buttons` | `options: [{label, value}]` | row of pill buttons; tap sends `label` as the user's text and `value` as payload; all buttons in the row disable after one tap |
| `card` | `title?`, `items: [{title, subtitle?, ltr?: bool}]`, `footer?` | bordered box with stacked item rows (site list, needs list) |
| `table` | `title?`, `columns: [str]`, `rows: [[str]]` | small table ("deploy day vs now") |
| `recommendation` | `title`, `lines: [{label, value}]`, `total`, `reason` | bordered card with label/value rows, a bold total and one reasoning sentence |
| `note` | `tone: success\|info\|warning`, `text` | tinted one-line note (green for "you don't need to do anything") |
| `approval` | `action_id`, `title`, `lines: [str]`, `cost?`, `downtime?`, `state: proposed\|approved\|rejected\|expired\|running\|succeeded\|failed\|unknown` | amber card "تأیید شما لازم است", Confirm/Cancel buttons only while `proposed` |
| `progress` | `job_id`, `steps: [{label, state: waiting\|active\|done\|failed}]`, `done: bool` | step list with icons; live-updated over SSE |
| `code` | `text` | LTR monospace block |
| `error` | `text`, `next?` | red bubble: one sentence + what to do next |
| `steps` | `items: [str]` | numbered list (API-key how-to) |
| `link` | `href`, `label` | LTR link that opens in a new tab |
| `divider` | `text` | centred grey date divider ("۳ روز بعد از دیپلوی") |

`Message.text` is the bubble's paragraph text (may be empty when only blocks).
Bot bubbles are on the right in RTL (start side); user bubbles are blue on the left.

## HTTP endpoints (all login-required, JSON, CSRF via `X-CSRFToken`)

- `GET /` — chat page, all messages server-rendered.
- `POST /chat/send` — body `{text, value?}` → `{messages: [{id, html}], composer}`.
- `POST /chat/key` — body `{key}` (only while composer is `password`) → same shape. Never logged, never stored in `Message`.
- `POST /chat/upload` — multipart `file` (.zip) → same shape.
- `POST /chat/approve` — body `{action_id, decision: "confirm"|"cancel"}` → same shape.
- `POST /chat/reset` — clears the thread and re-greets → same shape plus `reset: true`.
- `GET /chat/stream?job=<id>` — SSE. Event `progress`, data `{message_id, html, done}`. Closes when `done`.

`composer`: `{mode: "text"|"password", placeholder, label, attach: bool}`.
The page JS appends returned messages (or replaces `#msg-<id>` if it exists),
applies `composer`, and opens the SSE stream for every progress block whose
`done` is false (`data-job-id` on the block element).

Every message is rendered with `render_to_string("assistant/_message.html", {"m": message})`.
Its root element is `<div class="msg msg-{{ m.role }}" id="msg-{{ m.id }}">`.

## Liara client (`assistant/liara_client.py`)

`make_client(connection) -> LiaraClient` (returns `FakeLiaraClient` when
`settings.LIARA_FAKE`). All methods return plain normalized dicts/lists and raise
`LiaraError(code, status, message_fa)` with codes:
`unauthorized, not_found, frozen, rate_limited, unavailable, timeout, bad_request, unknown`.
No method retries a change. Every response passes through `redact_obj` before
being returned to callers that feed the LLM (`for_llm=True` helpers).
