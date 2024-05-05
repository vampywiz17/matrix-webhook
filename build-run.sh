#!/bin/bash

docker kill matrix-webhook
docker rm matrix-webhook
docker rmi matrix-webhook-image
docker build -t matrix-webhook-image .

docker compose up

# int_handler()
# {
    #echo "Gracefully stopping the container..."
    #docker stop av-app-container
    # echo "Killing the container..."
    # docker kill virtual-card-api-app-container
    # echo "Deleting the container..."
    # docker rm virtual-card-api-app-container
    # echo "All good!"
    # exit 1
# }
# trap 'int_handler' INT

#docker logs -f virtual-card-api-app-container
#docker logs -f nginx

# We never reach this part.
#exit 0