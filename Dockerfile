FROM python:3.9-slim AS builder

WORKDIR /app
ARG TARGETARCH

RUN apt-get update \
 && if [ "$TARGETARCH" = "arm" ]; then \
      apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev python3-dev gcc; \
    fi \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt ./

RUN pip wheel --prefer-binary --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.9-slim AS runtime

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LOGIN_STORE_PATH=/config

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-compile /wheels/*

COPY docker-entrypoint.sh ./docker-entrypoint.sh
COPY src/ ./src/
RUN chmod +x ./docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
