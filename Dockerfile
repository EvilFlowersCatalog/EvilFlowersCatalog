FROM python:3.9-alpine as builder

WORKDIR /usr/src/app

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# System setup
RUN apk update
RUN apk add --no-cache pkgconfig libffi-dev make gcc musl-dev python3-dev openssl-dev cargo postgresql-dev

# Copy source
COPY . .

# Dependencies
RUN pip install --user gunicorn
RUN pip install --user -r requirements.txt

FROM python:3.9-alpine

WORKDIR /usr/src/app

# Dependencies
RUN apk add --no-cache supervisor curl libpq redis
COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/src/app /usr/src/app

# Configuration
COPY conf/supervisor.conf /etc/supervisord.conf

# Execution
RUN chmod +x conf/entrypoint.sh
CMD ["conf/entrypoint.sh"]
