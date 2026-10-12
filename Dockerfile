# Dockerfile for phocinae-mcp — used by container-based MCP directory checks
# (Glama and similar) that build the image and verify the server starts and
# answers introspection requests.
#
# The server speaks JSON-RPC over stdio, so the container takes no port and no
# command arguments: a client launches it and talks over stdin/stdout.
#
# What works with no other services: initialize, tools/list, and any gate call
# that the local deterministic table decides (the L0 layer is offline by design).
# A gate call that reaches the grey zone needs a phocinae-server. Without one it
# fails CLOSED (decision=deny with a reason) — never a silent allow — which is
# the same behaviour the README documents.
#
#   docker build -t phocinae-mcp .
#   echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
#     | docker run -i --rm phocinae-mcp

FROM python:3.12-slim

# Pin the release so a directory check months from now tests the same artifact.
ARG PHOCINAE_MCP_VERSION=0.2.4
# Package index. Defaults to PyPI, which is what a directory check wants: it
# resolves the same artifact every other user gets. Override it when PyPI's file
# CDN is unreachable from the build host — which is a real situation, not a
# hypothetical: on the authoring network `pypi.org/simple/` answers but
# `files.pythonhosted.org` is blackholed, so the download step hangs or fails
# while the index itself looks healthy.
#
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=https://pypi.org/simple
# Point the server at a host-side decision service when one is available.
# 127.0.0.1 is the documented default and is correct for a local client that
# runs the server as a subprocess; inside a container that address is the
# container itself, so a container deployment must override this (for example
# --add-host=host.docker.internal:host-gateway and then
# -e PHOCINAE_MCP_SERVER=http://host.docker.internal:8155).
ENV PHOCINAE_MCP_SERVER=http://127.0.0.1:8155 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Frames are written as UTF-8 bytes regardless of locale. Setting these makes
    # that explicit for a slim image whose default locale is not UTF-8.
    PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8

RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
        --retries 5 --timeout 30 "phocinae-mcp==${PHOCINAE_MCP_VERSION}" \
    && python -c "import phocinae_mcp, sys; sys.stderr.write('phocinae-mcp %s installed\n' % phocinae_mcp.__version__)"

# The package ships no non-root user; drop privileges explicitly. A read-only
# stdio server needs no writable path at all.
RUN useradd --create-home --uid 10001 phocinae
USER phocinae
WORKDIR /home/phocinae

# No EXPOSE: the transport is stdio, not a socket.
# The check that this is a valid MCP server is that it answers a framed request
# on stdout, which the command below does.
ENTRYPOINT ["python", "-m", "phocinae_mcp"]
CMD []
