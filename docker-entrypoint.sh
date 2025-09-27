#!/bin/sh

if [ -f '.env' ]; then
    echo 'Environment file found, sourcing it...'
    set -a
    . ./.env
    set +a

    export PYTHON_LOG_LEVEL=debug
    export LOGIN_STORE_PATH=./store
fi

echo 'Starting the Python app...'

python src/main.py
STATUS=$?

echo "Python app exited with status $STATUS"

# prevent infinite restart loop
if [ $STATUS -ne 0 ]; then
    echo "App crashed. Not restarting. Exiting container."
    exit $STATUS
fi
