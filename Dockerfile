FROM alpine:3.14 as builder

WORKDIR /root

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# System setup
RUN apk update
RUN apk add --no-cache pkgconfig libffi-dev make gcc musl-dev python3 python3-dev openssl-dev cargo postgresql-dev curl py3-pip jpeg-dev zlib-dev

# Prepare Poetry
RUN python3 -m venv poetry
RUN env VIRTUAL_ENV=/root/poetry pip install poetry

WORKDIR /usr/src/app

# Copy source
COPY . .

# Dependencies
RUN env VIRTUAL_ENV=/root/poetry poetry export -f requirements.txt > requirements.txt
RUN pip3 install --user gunicorn
RUN pip3 install --user -r requirements.txt

FROM alpine:3.14

WORKDIR /usr/src/app

# Dependencies
RUN apk add --no-cache python3 supervisor curl libpq postgresql-client jpeg zlib py3-argon2-cffi
COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/src/app /usr/src/app

# Configuration
COPY conf/supervisor.conf /etc/supervisord.conf

# Execution
RUN chmod +x conf/entrypoint.sh
CMD ["conf/entrypoint.sh"]
