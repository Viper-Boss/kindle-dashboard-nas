FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8787 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apk upgrade --no-cache \
    && apk add --no-cache font-dejavu font-noto-cjk tzdata

RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip

COPY app.py proxy_admin.py admin_ui.py admin_extensions.py management.py maintenance.py traffic_stats.py web_auth.py service_watchdog.py ./

EXPOSE 8787
CMD ["python", "app.py"]
