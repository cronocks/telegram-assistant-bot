#!/bin/sh
set -e

# Ensure the database directory exists before restore/write.
mkdir -p "$(dirname "$SQLITE_PATH")"

# Restore the database from R2 on boot. -if-replica-exists makes the first-ever
# run a no-op (empty bucket): the bot then creates a fresh DB and runs migrations.
litestream restore -if-replica-exists -config /app/litestream.yml "$SQLITE_PATH"

# Run the bot under Litestream so the WAL is continuously replicated to R2.
exec litestream replicate -config /app/litestream.yml \
    -exec "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
