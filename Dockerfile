FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# PORT comes from fly.toml's [env], so a shell expands it; `exec` then
# makes uvicorn the main process, so Fly's stop signal reaches it and it
# shuts down cleanly. One uvicorn process on purpose: the pipeline runner's
# lock works within a process. --proxy-headers so the app sees Fly's HTTPS
# scheme and the client's address.
CMD ["sh", "-c", "exec uvicorn web.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
