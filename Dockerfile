FROM python:3.12-slim
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src
ENTRYPOINT ["python", "-m", "media_sync_manager"]
CMD ["run", "--config", "/etc/media-sync-manager/config.yaml"]
