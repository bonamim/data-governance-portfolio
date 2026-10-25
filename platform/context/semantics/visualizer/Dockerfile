# Ossie Visualizer (github.com/blankmeadow/ossie-visualizer, Apache-2.0): a browser-only viewer for
# Apache Ossie documents. Built from a pinned upstream commit, its source verified by checksum, and
# served as static files — no code of ours, nothing vendored. Upgrading = new commit + checksum.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
ARG COMMIT=d6368a3b479b2e567a11748fc98e2cb70bdc5e75
ARG SHA256=a916a12299d1e390b8ba4cdef5bc21061890de226ecc1124c2dea65fb6b813c1
WORKDIR /src
RUN wget -qO /tmp/src.tar.gz "https://github.com/blankmeadow/ossie-visualizer/archive/${COMMIT}.tar.gz" \
 && echo "${SHA256}  /tmp/src.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/src.tar.gz --strip-components=1 \
 && npm ci --no-audit --no-fund \
 && npm run build

FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
COPY --from=build /src/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html/ossie-visualizer
COPY --from=build /src/LICENSE /src/NOTICE /usr/share/licenses/ossie-visualizer/
