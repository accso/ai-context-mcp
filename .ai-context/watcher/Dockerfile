FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY *.py .

# Pre-create the mount-point directories. Some container runtimes do not
# auto-create the parent directory for bind mounts, which leads to missing
# /workspace/<project> paths inside the consolidated watcher container.
RUN mkdir -p /workspace /extra-gitignores

# Default env — overridden by docker-compose
ENV CONFIG_PATH=/app/config.yaml
ENV PROJECT_ROOT=/workspace

ENTRYPOINT ["python", "multi_entrypoint.py"]

