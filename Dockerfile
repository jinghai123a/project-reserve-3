FROM node:22.23.4-bookworm-slim AS node-tools
ARG CODEX_VERSION=0.162.1
RUN npm install --global --omit=dev "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

# The queue lock is resolved for Python 3.12. Debian's default Python 3.11
# activates additional conditional dependencies, so keep the runtime aligned.
FROM python:3.12.14-slim-bookworm
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates tini libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 node \
    && useradd --uid 1000 --gid node --create-home node \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node -e 'const fs=require("fs"),p="/usr/local/lib/node_modules/@openai/codex",m=require(p+"/package.json");fs.symlinkSync(p+"/"+(typeof m.bin==="string"?m.bin:m.bin.codex),"/usr/local/bin/codex")' \
    && python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data/home CODEX_HOME=/data/home/.codex \
    CLOUDKIT_RUNS_ROOT=/data/runs GIT_TERMINAL_PROMPT=0
WORKDIR /opt/cloudkit
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --require-hashes -r src/cloudkit/assets/queue/requirements.lock \
    && pip install --no-cache-dir . \
    && python -c 'import tarfile; assert hasattr(tarfile, "data_filter")' \
    && python -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
    && node --version && npm --version && codex --version
COPY deploy /opt/cloudkit/deploy
COPY .codex/config.toml /opt/cloudkit/deploy/company-rules/.codex/config.toml
COPY .codex/agents /opt/cloudkit/deploy/company-rules/.codex/agents
COPY company/organization.json /opt/cloudkit/deploy/company-rules/company/organization.json
COPY docs/WORKFLOW.zh-CN.md /opt/cloudkit/docs/WORKFLOW.zh-CN.md
RUN chmod 755 /opt/cloudkit/deploy/entrypoint.sh /opt/cloudkit/deploy/git-askpass.sh
USER node
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/cloudkit/deploy/entrypoint.sh"]
CMD ["worker"]
