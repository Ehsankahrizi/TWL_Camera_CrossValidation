# Image for the ECS service (crossval.runner). Built by .github/workflows/docker.yml
# and published to ghcr.io/ehsankahrizi/twl_camera_crossvalidation.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

COPY crossval ./crossval

ENV PYTHONUNBUFFERED=1 \
    STATE_DIR=/data/state \
    CAPTURE_DIR=/data/state/events

CMD ["python", "-m", "crossval.runner"]
