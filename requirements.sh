#!/usr/bin/env sh
poetry export -f requirements.txt --without-hashes --output requirements.txt -E s3 -E pdf -E apm -E worker
