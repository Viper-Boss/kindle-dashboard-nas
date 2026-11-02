FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8

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
