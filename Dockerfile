FROM python:3.12-slim as builder

# System setup
RUN apt update -y
RUN apt install -y libffi-dev build-essential libsasl2-dev libpq-dev libjpeg-dev libldap-dev

# https://github.com/python-ldap/python-ldap/issues/432
RUN echo 'INPUT ( libldap.so )' > /usr/lib/libldap_r.so

WORKDIR /usr/src/app

# Copy source
COPY . .

## Python environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Dependencies
RUN pip install --user gunicorn wheel --no-cache-dir
RUN pip install --user -r requirements.txt --no-cache-dir

FROM python:3.12-slim

# Dependencies
RUN apt update -y
RUN apt install -y supervisor curl postgresql-client libjpeg-tools argon2 tzdata ldap-utils swig cron

WORKDIR /usr/src/app

COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/src/app /usr/src/app
ENV PATH=/root/.local/bin:$PATH

RUN echo "0.6.0" > VERSION.txt
RUN date -I > BUILD.txt

# Configuration
COPY conf/supervisor.conf /etc/supervisord.conf
RUN chmod +x conf/entrypoint.sh

# Health check
HEALTHCHECK CMD curl --fail http://localhost:8000/api/v1/status || exit 1

# Execution
CMD ["conf/entrypoint.sh"]
