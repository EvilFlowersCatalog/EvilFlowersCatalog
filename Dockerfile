FROM python:3-slim-buster
ENV PYTHONUNBUFFERED=1
WORKDIR /code

# System setup
RUN apt update
RUN apt upgrade -y
RUN apt install curl -y

# Copy source
COPY . /code/

# Dependencies
RUN pip install -U pip
RUN pip install -U gunicorn
RUN pip install -r requirements.txt
