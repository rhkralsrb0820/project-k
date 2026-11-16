# Container image for Project K.
FROM python:3.12-slim
# Korean font for PDF reports (Nanum Gothic)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY . /srv
RUN pip install --no-cache-dir -r "/srv/Project K/requirements.txt"
ENV PYTHONUTF8=1 \
    PROJECTK_FONT=/usr/share/fonts/truetype/nanum/NanumGothic.ttf \
    PROJECTK_FONT_BOLD=/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf
EXPOSE 8080
# Listens on $PORT (default 8080). torch is not installed; the optional LoRA/CUDA tools report 'environment missing' without it.
CMD ["sh", "-c", "exec python -m uvicorn app.server:app --app-dir '/srv/Project K' --host 0.0.0.0 --port ${PORT:-8080}"]
