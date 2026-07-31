FROM python:3.12-alpine

# openssh-client is for `cud add <host>` from the CLI; su-exec is how the
# entrypoint drops root after fixing up the socket group. Everything else in
# the project is the standard library.
RUN apk add --no-cache openssh-client su-exec

RUN adduser -S -u 10001 -h /home/cud cud \
 && mkdir -p /home/cud/.ssh \
 && chown -R cud /home/cud \
 && chmod 700 /home/cud/.ssh

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
