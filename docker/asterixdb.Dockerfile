# A single-node AsterixDB sample cluster for CI.
#
# Boots the sample cluster on start and serves the query service on 19002. Used
# by the CI workflows as a `services:` container so a pull request gets a
# cluster in seconds instead of downloading and unpacking an assembly per run.
#
# Build (pin the version to the cluster you want to test against):
#   docker build -f docker/asterixdb.Dockerfile \
#     --build-arg ASTERIXDB_SERVER_URL=<server-assembly-zip> -t asterixdb-ci .
FROM eclipse-temurin:17-jdk

ARG ASTERIXDB_SERVER_URL=https://archive.apache.org/dist/asterixdb/asterixdb-0.9.9/apache-asterixdb-0.9.9-server.zip

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip openssh-server openssh-client \
    && rm -rf /var/lib/apt/lists/*

# The sample cluster launches its node over ssh to localhost; set up
# passwordless localhost access for the runtime user.
RUN ssh-keygen -t rsa -N "" -f /root/.ssh/id_rsa \
    && cat /root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys \
    && printf 'Host localhost 127.0.0.1\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n' \
       >> /root/.ssh/config

WORKDIR /opt/asterixdb
RUN curl -fsSL "$ASTERIXDB_SERVER_URL" -o server.zip \
    && unzip -q server.zip \
    && rm server.zip

EXPOSE 19001 19002

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

HEALTHCHECK --interval=5s --timeout=5s --retries=30 --start-period=30s \
    CMD curl -sf -X POST http://localhost:19002/query/service \
        --data-urlencode 'statement=SELECT 1;' | grep -q '"status": "success"' || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
