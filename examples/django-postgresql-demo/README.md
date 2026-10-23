# PostgreSQL booking demo

This is authored sample code, separate from the assistant's static inspector. The assistant never executes uploaded source. Run this example yourself to test a small Django application and persistence:

```bash
docker compose up --build
```

Open http://localhost:8001/, add a booking, restart Compose and confirm it remains. `/health/` observes database connectivity; it does not test booking behavior. `docker compose down` retains the named database volume. Removing that volume deletes your test bookings.

```bash
docker compose run --rm web python manage.py test bookings --settings=demo.test_settings
```

The five model/view checks use isolated SQLite; the Compose journey is the separate PostgreSQL integration check. The agent ran the former on a Django 6.0.5 compatibility runtime; image builds, pinned dependencies and PostgreSQL execution remain unverified in its sandbox.

Upload `django-postgresql-demo.zip` from the assistant's sample download. Choose fresh data, new PostgreSQL, no uploads and `/health/`; a complete sample is now available for review. Static recognition and local test receipts do not deploy it.

For an eventual real deployment, generate a new SECRET_KEY, provide ALLOWED_HOSTS and DB_NAME/DB_USER/DB_PASSWORD/DB_HOST/DB_PORT through protected server configuration, and initialize only the newly created database through the verified provider mechanism. The public development values in Compose are for this local example only. The assistant's live provisioning/configuration/initialization integration is still pending. Do not include a real environment file or credentials in your ZIP.

Dependency versions were checked against [Psycopg on PyPI](https://pypi.org/project/psycopg/3.3.6/) and [Gunicorn on PyPI](https://pypi.org/project/gunicorn/26.2.0/) on 23 October 2026. Django follows the assistant's existing pin.
