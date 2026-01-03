FROM python:3.14-slim AS builder

<<<<<<< Updated upstream
# System setup
RUN apt update -y && apt install -y git libffi-dev build-essential libsasl2-dev libjpeg-dev libldap-dev  \
    postgresql-common libxml2-dev libxslt1-dev && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y && \
    apt update -y && \
    apt install -y postgresql-client-17 postgresql-server-dev-17
=======
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
>>>>>>> Stashed changes

RUN echo 'INPUT ( libldap.so )' > /usr/lib/libldap_r.so

WORKDIR /usr/src/app

COPY requirements.txt requirements.txt

ENV CFLAGS="-DINT64CONST(n)=n##LL -DUINT64CONST(n)=n##ULL"

RUN pip install --user -r requirements.txt --no-cache-dir

<<<<<<< Updated upstream
FROM python:3.14-slim
=======

FROM python:3.13-slim
>>>>>>> Stashed changes

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    else \
        sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list; \
    fi && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update -y && apt-get install -y --no-install-recommends \
    supervisor curl libjpeg-tools argon2 tzdata ldap-utils swig postgresql-client && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY . .
COPY --from=builder /root/.local /root/.local

ENV PATH=/root/.local/bin:$PATH
ENV GUNICORN_CMD_ARGS=''
ENV LOGLEVEL=info

RUN date -I > BUILD.txt

COPY conf/supervisor.conf /etc/supervisord.conf
RUN chmod +x conf/entrypoint.sh

HEALTHCHECK CMD curl --fail http://localhost:8000/api/v1/status || exit 1

CMD ["conf/entrypoint.sh"]
