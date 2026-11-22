FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=pip_ca if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi && python -m pip install -r requirements.txt
COPY --chown=10001:10001 bot.py info.py Script.py ./
COPY --chown=10001:10001 database/ database/
COPY --chown=10001:10001 plugins/ plugins/
USER 10001:10001
EXPOSE 8080
CMD ["python", "bot.py"]
