FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py ./

# Run as a non-root user
RUN useradd --create-home --shell /usr/sbin/nologin bridge
USER bridge

ENTRYPOINT ["python", "/app/bridge.py"]
