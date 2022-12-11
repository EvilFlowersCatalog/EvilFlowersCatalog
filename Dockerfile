FROM alpine:3.17 as builder

WORKDIR /root

# System setup
RUN apk update
RUN apk add --no-cache pkgconfig libffi-dev make gcc musl-dev python3 python3-dev openssl-dev cargo postgresql-dev curl py3-pip jpeg-dev zlib-dev openldap-dev

# https://github.com/python-ldap/python-ldap/issues/432
RUN echo 'INPUT ( libldap.so )' > /usr/lib/libldap_r.so

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
RUN pip install gunicorn wheel --no-cache-dir
RUN pip install -r requirements.txt --no-cache-dir

FROM alpine:3.17

WORKDIR /usr/src/app

RUN echo "0.5.0" > VERSION.txt
RUN date -I > BUILD.txt

# Dependencies
RUN apk add --no-cache python3 supervisor curl libpq postgresql-client jpeg zlib py3-argon2-cffi tzdata libldap
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/src/app /usr/src/app

# Prepare virtual env
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Configuration
COPY conf/supervisor.conf /etc/supervisord.conf

# Health check
HEALTHCHECK CMD curl --fail http://localhost:8000/api/v1/status || exit 1

# Execution
RUN chmod +x conf/entrypoint.sh
CMD ["conf/entrypoint.sh"]
