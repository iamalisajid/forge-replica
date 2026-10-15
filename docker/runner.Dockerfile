# Scenario runner: forge's interpreter + Playwright, run inside the replica's internal network.
# The image is single-purpose, so packages go into the system interpreter (it ships without venv support).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PIP_BREAK_SYSTEM_PACKAGES=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir playwright==1.63.0 pydantic==2.14.1 pyyaml==6.0.3
WORKDIR /runner
COPY forge/__init__.py forge/__init__.py
COPY forge/models forge/models
COPY forge/stages/__init__.py forge/stages/__init__.py
COPY forge/stages/oracle forge/stages/oracle
ENTRYPOINT ["python3", "-m", "forge.stages.oracle.runner_cli"]
