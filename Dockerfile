FROM python:3.15-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211

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

COPY app.py proxy_admin.py admin_ui.py admin_extensions.py management.py maintenance.py traffic_stats.py ./

EXPOSE 8787
CMD ["python", "app.py"]
