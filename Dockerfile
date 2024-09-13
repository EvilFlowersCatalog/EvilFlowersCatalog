FROM python:3.12-slim AS builder

# System setup
RUN apt update -y && apt install -y libffi-dev build-essential libsasl2-dev libpq-dev libjpeg-dev libldap-dev

# https://github.com/python-ldap/python-ldap/issues/432
RUN echo 'INPUT ( libldap.so )' > /usr/lib/libldap_r.so

WORKDIR /usr/src/app

# Copy source
COPY requirements.txt requirements.txt

# Dependencies
RUN pip install --user -r requirements.txt --no-cache-dir

FROM python:3.12-slim

## Python environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Dependencies
RUN apt update -y && apt install -y supervisor curl postgresql-client libjpeg-tools argon2 tzdata ldap-utils swig

WORKDIR /usr/src/app

COPY . .
COPY --from=builder /root/.local /root/.local

ENV PATH=/root/.local/bin:$PATH

RUN date -I > BUILD.txt

# Configuration
COPY conf/supervisor.conf /etc/supervisord.conf
RUN chmod +x conf/entrypoint.sh

# Health check
HEALTHCHECK CMD curl --fail http://localhost:8000/api/v1/status || exit 1

# Execution
CMD ["conf/entrypoint.sh"]
