# One image for every component — the compose service picks the command.
FROM python:3.12-slim

# iproute2 provides `tc` for the optional netem network-shaping experiments
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Patient pip (slow links): long timeout, many retries, one layer per package
# so completed downloads are cached across build retries.
RUN pip install --no-cache-dir --timeout 180 --retries 10 redis
RUN pip install --no-cache-dir --timeout 180 --retries 10 numpy
RUN pip install --no-cache-dir --timeout 180 --retries 10 scipy

WORKDIR /app
COPY *.py /app/

# default command is overridden per service in docker-compose.yml
CMD ["python", "-c", "print('specify a command in docker-compose.yml')"]
