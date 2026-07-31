FROM python:3.12-alpine

# su-exec lets the entrypoint drop root once the socket group is sorted;
# nsenter (util-linux-misc) runs OS updates in the host's namespaces.
# Everything else is the standard library.
RUN apk add --no-cache su-exec util-linux-misc

RUN adduser -S -u 10001 -H cud

WORKDIR /app
COPY agent/ agent/
COPY dashboard/ dashboard/
COPY cud ./

# config.json holds agent bearer tokens and is rewritten at 0600; the registry
# cache is written beside it. Both belong on a volume, or every container
# replacement loses the registered hosts and re-warms the cache from scratch.
ENV CUD_CONFIG=/config/config.json
RUN mkdir -p /config && chown cud /config
VOLUME /config

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# No USER here on purpose: the entrypoint starts as root only to join the
# mounted socket's group -- whatever its id happens to be on your host -- and
# then drops to `cud` with su-exec. The server never runs as root.
ENV APP_USER=cud
EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD \
  python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8500/healthz', timeout=4).status == 200 else 1)"

# The image is the CLI, so `docker run … add --local` and `… check` work too.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh", "python3", "/app/cud"]
CMD ["serve"]
