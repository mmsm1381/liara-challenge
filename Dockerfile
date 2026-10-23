FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_DEBUG=0

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

# Build-only key so settings load during collectstatic. Never used at runtime.
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

# Non-root runtime user. /app/var holds uploads and the fake Liara state.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app \
 && mkdir -p /app/var \
 && chown app:app /app/var

USER app

EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120"]
