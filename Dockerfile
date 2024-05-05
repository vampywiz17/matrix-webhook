FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt ./

RUN apt-get update && apt-get install -y cmake

# build dependencies
RUN set -x \
    && pip install --no-cache-dir -r requirements.txt
   

EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENV LOGIN_STORE_PATH=/config


COPY docker-entrypoint.sh ./
ENTRYPOINT [ "./docker-entrypoint.sh" ]

COPY src/ ./src/