#!/usr/bin/env sh
poetry export -f requirements.txt --without-hashes --output requirements.txt --with s3 --with pdf --with apm --with docker --with kafka
