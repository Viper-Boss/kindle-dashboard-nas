FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
RUN apk upgrade --no-cache \
    && python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY requirements.txt /tmp/proxy-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/proxy-requirements.txt
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip /tmp/proxy-requirements.txt
WORKDIR /app
