FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
COPY . /src
# Quotes matter: unquoted [web] is a shell glob. One image serves both the poller and the editor.
RUN pip install --no-cache-dir "/src[web]" && rm -rf /src
EXPOSE 8087
ENTRYPOINT ["python", "-m", "media_sync_manager"]
# `--config` is a TOP-LEVEL argument, so it must precede the subcommand. `run --config …` is an
# argparse error (exit 2) and the container never starts. Locked by
# tests/test_cli.py::test_shipped_container_commands_parse.
CMD ["--config", "/etc/media-sync-manager/config.yaml", "run"]
