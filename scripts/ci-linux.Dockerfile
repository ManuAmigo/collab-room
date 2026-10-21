# Linux for scripts/ci --linux: tmux and Pythons 3.9, 3.11–3.13 installed outside home and owned by
# root — where collab trusts an interpreter (trusted_interpreter); the tests run as an ordinary user.
# /usr/bin/python3 is the 3.9, as on a Mac: room windows run «python3 collab» with PATH narrowed to
# /usr/bin:/bin, and the hand-over test needs that python3 to be old. For the run,
# /opt/pyv/<version>/python3 — root's folders, not a link in the user's home collab would not trust.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends tmux git ca-certificates curl lsof tzdata procps \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.9 3.11 3.12 3.13 \
    && for v in 3.9 3.11 3.12 3.13; do \
        ln -s "$(uv python find "$v")" "/usr/local/bin/python$v" \
        && mkdir -p "/opt/pyv/$v" && ln -s "$(uv python find "$v")" "/opt/pyv/$v/python3"; \
    done \
    && ln -s "$(uv python find 3.9)" /usr/bin/python3
RUN useradd -m ci
COPY ci-linux-entry.sh /usr/local/bin/ci-linux-entry
USER ci
ENTRYPOINT ["/usr/local/bin/ci-linux-entry"]
