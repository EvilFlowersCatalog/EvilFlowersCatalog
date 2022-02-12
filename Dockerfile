FROM alpine:3.15 as builder

WORKDIR /root

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# System setup
RUN apk update
RUN apk add --no-cache pkgconfig libffi-dev make gcc musl-dev python3 python3-dev openssl-dev cargo postgresql-dev curl py3-pip jpeg-dev zlib-dev

# Prepare Poetry
RUN python3 -m venv poetry
RUN env VIRTUAL_ENV=/root/poetry pip install wheel --no-cache-dir
RUN env VIRTUAL_ENV=/root/poetry pip install poetry --no-cache-dir

WORKDIR /usr/src/app

# Copy source
COPY . .

# Prepare virtual env
ENV VIRTUAL_ENV=/opt/venv
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Dependencies
RUN env VIRTUAL_ENV=/root/poetry poetry export -f requirements.txt > requirements.txt
RUN pip install gunicorn wheel --no-cache-dir
RUN pip install -r requirements.txt --no-cache-dir

FROM alpine:3.15

ENV VERSION="0.4.0"

WORKDIR /usr/src/app

# Dependencies
RUN apk add --no-cache python3 supervisor curl libpq postgresql-client jpeg zlib py3-argon2-cffi tzdata
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/src/app /usr/src/app

# Prepare virtual env
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Configuration
COPY conf/supervisor.conf /etc/supervisord.conf

# Execution
RUN chmod +x conf/entrypoint.sh
CMD ["conf/entrypoint.sh"]
