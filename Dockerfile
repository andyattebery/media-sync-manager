FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY . /src
# Quotes matter: unquoted [web] is a shell glob. One image serves both the poller and the editor.
RUN pip install --no-cache-dir "/src[web]" && rm -rf /src
EXPOSE 8087
ENTRYPOINT ["python", "-m", "media_sync_manager"]
CMD ["run", "--config", "/etc/media-sync-manager/config.yaml"]
