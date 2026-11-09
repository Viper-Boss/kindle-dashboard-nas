FROM golang:1.27-alpine@sha256:fa3d596898d9f126af7c01d14ee7597bab3f39cccf392ef36b69fe77cd681f18 AS build
ARG MIHOMO_COMMIT=3f27ef39f8c96bbfbc8b0b9bb10f5c9ef60149d8
ARG MIHOMO_SOURCE_SHA256=ecda5f8c8a8a1d60bc088c2640c85d92c8f002c0686234a26bdf671735afe216
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOMAXPROCS=2 GOMEMLIMIT=1400MiB
RUN apk add --no-cache git ca-certificates tzdata
WORKDIR /src
RUN (wget -q -O /tmp/source.tar.gz "https://codeload.github.com/MetaCubeX/mihomo/tar.gz/${MIHOMO_COMMIT}" \
      || HTTPS_PROXY= HTTP_PROXY= https_proxy= http_proxy= wget -q -O /tmp/source.tar.gz "https://codeload.github.com/MetaCubeX/mihomo/tar.gz/${MIHOMO_COMMIT}") \
    && echo "${MIHOMO_SOURCE_SHA256}  /tmp/source.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/source.tar.gz --strip-components=1
COPY patch-dependencies.sh /tmp/patch-dependencies.sh
RUN sh /tmp/patch-dependencies.sh
COPY tools/patch-traffic/main.go /tmp/patch-traffic.go
COPY traffic/client_traffic.go tunnel/statistic/client_traffic_kindle.go
COPY traffic/client_traffic_test.go tunnel/statistic/client_traffic_kindle_test.go
RUN go run /tmp/patch-traffic.go \
    && go test ./tunnel/statistic -run '^Test(ShortConnectionsAndMidnight|ConcurrentByteAccounting)$'
RUN go build -tags with_gvisor -trimpath -buildvcs=false \
       -ldflags '-s -w -X github.com/metacubex/mihomo/constant.Version=v1.19.33-security.3' -o /out/mihomo .
COPY healthcheck.go /tmp/healthcheck.go
RUN go build -trimpath -o /out/healthcheck /tmp/healthcheck.go

FROM scratch
ENV HOME=/root TZ=Asia/Shanghai
LABEL org.opencontainers.image.source="https://github.com/Viper-Boss/kindle-dashboard-nas" \
      org.opencontainers.image.upstream="https://github.com/MetaCubeX/mihomo" \
      org.opencontainers.image.licenses="AGPL-3.0-only AND GPL-3.0-only"
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /usr/share/zoneinfo /usr/share/zoneinfo
COPY --from=build /out/mihomo /mihomo
COPY --from=build /out/healthcheck /healthcheck
COPY --from=build /src/LICENSE /LICENSE.mihomo
COPY LICENSE.kindle /LICENSE.kindle
WORKDIR /root/.config/mihomo
ENTRYPOINT ["/mihomo"]
