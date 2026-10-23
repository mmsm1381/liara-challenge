# Deploying the Liara Assistant on Liara

The assistant runs as a Docker platform app built from the repository root `Dockerfile`. It needs three pieces: the web app, a PostgreSQL database, and a Redis instance. Background jobs run in a separate worker.

## 1. Create the resources

1. Create a **Docker** app named `liara-assistant` (the name in `liara.json`) and set its port to `8000`.
2. Create a **PostgreSQL** database and a **Redis** instance in the same region.
3. Note the connection URLs. Liara shows them in each resource's settings.

## 2. Set environment variables on the web app

Set these in the app's environment settings. Do not commit real values.

| Variable | Value |
| --- | --- |
| `DJANGO_SECRET_KEY` | A new long random string |
| `DJANGO_DEBUG` | `0` |
| `DJANGO_ALLOWED_HOSTS` | Your Liara hostname, e.g. `liara-assistant.liara.run` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://liara-assistant.liara.run` |
| `DATABASE_URL` | PostgreSQL URL from step 1 |
| `REDIS_URL` | Redis URL from step 1 |
| `TOKEN_ENCRYPTION_KEY` | Fernet key (see below) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TOOL_MODE` | Liara AI settings |

Generate the Fernet key locally:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The web container runs `migrate` on start, then gunicorn.

## 3. Run the worker

Background jobs need a worker process that reads the same `REDIS_URL` and `DATABASE_URL`. Use either option:

- **Separate app (recommended):** create a second Docker app from the same image and set its command override to `python manage.py rqworker default`. Give it the same environment variables.
- **Single process (demo only):** set `RQ_SYNC=1`. Jobs then run inline in the web process and no worker app is needed. Do not use this for real traffic.

## 4. Verify

Open `https://<your-host>/` and submit a request. If jobs stay queued, check that the worker app is running and that it uses the same `REDIS_URL`.
