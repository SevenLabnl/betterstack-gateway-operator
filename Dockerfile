# Two stages so the wheels are built somewhere that has a compiler and the runtime
# image does not need one.
FROM python:3.13-alpine AS build

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-alpine

# Runs as nobody. Nothing here needs to write to disk, so the deployment mounts the
# root filesystem read-only as well.
RUN adduser -D -u 65532 operator
COPY --from=build /install /usr/local
USER 65532

# kopf discovers handlers by importing the module named here
ENTRYPOINT ["kopf", "run", "--standalone", "--all-namespaces", \
            "--liveness=http://0.0.0.0:8080/healthz", \
            "-m", "gateway_uptime.operator"]
