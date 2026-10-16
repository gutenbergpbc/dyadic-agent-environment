FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7
RUN groupadd -g 1200 workplace && useradd -m -u 1100 -g workplace app && useradd -m -u 1101 -g workplace agent_a && useradd -m -u 1102 -g workplace agent_b && chmod 700 /home/agent_a /home/agent_b && mkdir -p /workspace/data /control && chmod 700 /control
COPY fixture/app.py fixture/README.md /workspace/
COPY fixture/seed.py fixture/supervisor.py /opt/workplace/
COPY fixture/appctl /usr/local/bin/appctl
RUN python /opt/workplace/seed.py && chown -R app:workplace /workspace && chmod -R g+rwX /workspace && chmod g+s /workspace /workspace/data && chmod 755 /usr/local/bin/appctl
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
CMD ["python", "-I", "/opt/workplace/supervisor.py"]
