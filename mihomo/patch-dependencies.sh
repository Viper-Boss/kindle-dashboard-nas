#!/bin/sh
set -eu
# Upstream v1.19.33 pins old Go modules for Go 1.20 compatibility. This build
# targets Go 1.27 and retains the upstream gVisor feature while applying fixes.
go get github.com/klauspost/compress@v1.18.7 \
  golang.org/x/crypto@v0.58.0 \
  golang.org/x/net@v0.62.0 \
  golang.org/x/sys@v0.49.0 \
  golang.org/x/text@v0.43.0 \
  github.com/insomniacslk/dhcp@v0.0.0-20260719225207-c76316d4aa82
go mod tidy
