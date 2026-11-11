# Мира в режиме server. Только стандартная библиотека Python - ставить нечего.
FROM python:3.14-slim

# Байткод не пишем: корень контейнера только для чтения (read_only в
# docker-compose.yml). Вывод без буфера - иначе журнал docker отстаёт.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY wam ./wam
COPY bot ./bot
COPY demo ./demo
COPY web ./web

# Не root: сервису незачем владеть контейнером. Пишет он только в /app/data -
# это том ./data, на сервере его каталог отдан uid 10001 (см. README).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mira \
    && mkdir -p /app/data \
    && chown mira:mira /app/data \
    && chmod 700 /app/data
USER 10001:10001

EXPOSE 8765
CMD ["python", "-m", "web.server"]
