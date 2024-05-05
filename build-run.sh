#!/bin/bash

docker kill matrix-webhook
docker rm matrix-webhook
docker rmi matrix-webhook-image
docker build -t matrix-webhook-image .

docker compose up