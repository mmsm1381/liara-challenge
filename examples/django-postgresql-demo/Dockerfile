FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
USER 1000:1000
EXPOSE 8000
CMD ["gunicorn", "demo.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--access-logfile", "-"]
