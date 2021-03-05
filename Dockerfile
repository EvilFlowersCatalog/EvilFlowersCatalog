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
RUN curl -sSL https://raw.githubusercontent.com/python-poetry/poetry/master/get-poetry.py | python -
ENV PATH="${PATH}:/root/.poetry/bin"
RUN pip install -U pip
RUN pip install -U gunicorn
RUN poetry export -f requirements.txt > requirements.txt
RUN pip install -r requirements.txt
RUN python manage.py migrate
RUN python manage.py loaddata users.json api_keys.json
RUN python manage.py basic_setup
