# The self-upgrader's Linux, for a Windows host (deploy/WINDOWS.md).
#
# The upgrader's walls are Linux sandboxes: bwrap for candidate code, and the Codex and Claude writers'
# own sandboxes for their shell commands. This image holds the same tools deploy/LAPTOP.md installs on a
# Linux host. `agentic-trading host container build` builds it with the project's dependencies; the
# project itself is the live checkout, mounted at run time, so the upgrader is always the live code.
FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git gh bubblewrap python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @openai/codex @anthropic-ai/claude-code \
 && npm cache clean --force

# The project's dependencies, read from the live checkout's pyproject.toml when the image was built
# (the image is tagged with that file's hash, so a dependency change means a rebuild).
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && mkdir -p /work && chown node:node /work \
 && git config --system --add safe.directory '*'
# (A Windows bind mount is owned by root in here; git would otherwise refuse the live checkout.)

# The writers' and gh's logins live in the volume mounted here, never in the image.
USER node
ENV HOME=/home/node PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
