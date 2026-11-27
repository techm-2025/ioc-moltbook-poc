# Moltbook <-> Mycelium bridge. Runs on the compose network next to the Mycelium backend, so it
# reaches it directly at http://mycelium-backend:8000 (no public tunnel) and keeps its state on a volume.
# Build context is this bridge/ folder (see docker-compose.yml); bridge/.dockerignore is an allow-list.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge_service.py summariser_module.py solver.py run_bridge.py ./
COPY tests/ ./tests/
RUN useradd -m -u 10001 bridge && mkdir -p /data && chown bridge /data
USER bridge
ENTRYPOINT ["python", "run_bridge.py"]
CMD ["--help"]
