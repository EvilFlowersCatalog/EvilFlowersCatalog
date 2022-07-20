#!/usr/bin/env sh
poetry export -f requirements.txt --without-hashes --output requirements.txt
