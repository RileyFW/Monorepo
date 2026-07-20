# syntax=docker/dockerfile:1
#
# glados-runner-base
# ------------------
# Phase 0 of the registry / base-image split (DEV / minikube only).
#
# This image captures the slow-changing "harness" layers of the runner:
#   * the python:3.9 interpreter
#   * system interpreters / libraries the runner needs at exec time
#       - libmagic1     (file-type identification via bytes)
#       - default-jdk   (running user-submitted Java / .jar targets)
#   * the runner's own Python dependencies (from Pipfile / Pipfile.lock)
#   * the runner harness source itself (COPY . /app)
#
# It is intentionally a faithful mirror of runner.Dockerfile's `base` +
# `python_dependencies` stages. It deliberately does NOT add or remove any
# runtime dependency-install behaviour -- that logic lives in runner.py and is
# out of scope until Phase 3.
#
# In a later phase a build-time Kaniko Job will build FROM this base image,
# layer the user-submitted Python dependencies on top, and push the result to
# the in-cluster registry (see kubernetes_init/registry/). runner.Dockerfile is
# left untouched and continues to build the current runner image on its own, so
# nothing here changes the existing build.

FROM python:3.9 AS base

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1

# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        # For file type identification via bytes
        libmagic1 \
        # Provides Java (JDK 11) for running user-submitted jar targets
        default-jdk

# Ability to pass in JVM options
ARG JAVA_OPTS
ENV JAVA_OPTS=$JAVA_OPTS

FROM base AS python_dependencies
# Copy in python requirements definitions and install the runner's own deps.
RUN pip install pipenv
COPY Pipfile .
COPY Pipfile.lock .

RUN pipenv install --system --deploy --ignore-pipfile

WORKDIR /app
COPY . /app
