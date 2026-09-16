FROM python:3.11-slim
WORKDIR /srv
ARG OTTOQ_CORE_REF=27d6571c43248e170523ec94e7c0279db0096fa4
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git clone --filter=blob:none https://github.com/OTTOYARD/otto-q-core.git /opt/otto-q-core \
 && git -C /opt/otto-q-core checkout "$OTTOQ_CORE_REF"
ENV OTTOQ_CORE_ROOT=/opt/otto-q-core
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
