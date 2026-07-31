FROM python:3.12-alpine

# The only runtime dependency in the whole project: `cud add <host>` shells out
# to ssh to provision a remote agent. Everything else is the standard library.
RUN apk add --no-cache openssh-client

RUN adduser -S -u 10001 -h /home/cud cud

WORKDIR /app
COPY agent/ agent/
COPY dashboard/ dashboard/
COPY cud setup-host.py ./

# config.json holds agent bearer tokens and is rewritten at 0600; the registry
# cache is written beside it. Both belong on a volume, or every container
# replacement loses the registered hosts and re-warms the cache from scratch.
ENV CUD_CONFIG=/config/config.json
RUN mkdir -p /config && chown cud /config
VOLUME /config

USER cud
EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD \
  python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8500/healthz', timeout=4).status == 200 else 1)"

# The image is the CLI, so `docker run … add --local` and `… check` work too.
ENTRYPOINT ["python3", "/app/cud"]
CMD ["serve"]
