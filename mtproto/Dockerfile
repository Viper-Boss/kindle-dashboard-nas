FROM python:3.15-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211
RUN apk upgrade --no-cache \
    && python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY requirements.txt /tmp/proxy-requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/proxy-requirements.txt
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip /tmp/proxy-requirements.txt
WORKDIR /app
