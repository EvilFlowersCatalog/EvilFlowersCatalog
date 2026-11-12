FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list; \
    fi && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    git libffi-dev build-essential libsasl2-dev libjpeg-dev libldap-dev \
    libpq-dev libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

RUN echo 'INPUT ( libldap.so )' > /usr/lib/libldap_r.so

WORKDIR /usr/src/app

COPY requirements.txt requirements.txt

ENV CFLAGS="-DINT64CONST(n)=n##LL -DUINT64CONST(n)=n##ULL"

RUN pip install --user -r requirements.txt --no-cache-dir


FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list; \
    fi && \
    rm -rf /var/lib/apt/lists/*

ARG PG_MAJOR=18

# PostgreSQL client tools (psql, pg_dump) from the official PGDG repository: pg_dump refuses to dump
# a server newer than itself, and Debian's own postgresql-client lags behind the server we run.
RUN apt-get update -y && apt-get install -y --no-install-recommends curl && \
    install -d /usr/share/postgresql-common/pgdg && \
    curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc && \
    . /etc/os-release && \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    supervisor curl libjpeg-tools argon2 tzdata ldap-utils swig postgresql-client-${PG_MAJOR} fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY . .
COPY --from=builder /root/.local /root/.local

ENV PATH=/root/.local/bin:$PATH
ENV GUNICORN_CMD_ARGS=''
ENV LOGLEVEL=info
# Celery node name of the catalog worker (#106). supervisord refuses to start
# when %(ENV_CELERY_HOSTNAME)s is unset, hence the default; Celery expands %h
# to the container hostname. k8s sets a stable name (catalog@dev).
ENV CELERY_HOSTNAME=catalog@%h

RUN date -I > BUILD.txt

COPY conf/supervisor.conf /etc/supervisord.conf
RUN chmod +x conf/entrypoint.sh

HEALTHCHECK CMD curl --fail http://localhost:8000/api/v1/status || exit 1

CMD ["conf/entrypoint.sh"]
