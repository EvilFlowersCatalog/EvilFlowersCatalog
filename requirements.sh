#!/usr/bin/env sh
poetry export -f requirements.txt --without-hashes --output requirements.txt --with logfire --with docker --with s3 --with pdf
