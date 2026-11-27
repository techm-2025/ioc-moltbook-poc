# Moltbook <-> Mycelium bridge. Runs on the compose network next to the Mycelium backend, so it
# reaches it directly at http://mycelium-backend:8000 (no public tunnel) and keeps its state on a volume.
# Build context is the repo root (see docker-compose.yml); .dockerignore is an allow-list.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge_service.py summariser_module.py ./
COPY bridge/run_bridge.py ./
RUN useradd -m -u 10001 bridge && mkdir -p /data && chown bridge /data
USER bridge
ENTRYPOINT ["python", "run_bridge.py"]
CMD ["--help"]
