FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
RUN apk upgrade --no-cache \
    && python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY requirements.txt /tmp/proxy-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/proxy-requirements.txt
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip /tmp/proxy-requirements.txt
WORKDIR /app
