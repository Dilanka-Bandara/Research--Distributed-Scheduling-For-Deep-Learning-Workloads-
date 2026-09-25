# One image for every component — the compose service picks the command.
#
# V09: dependency versions are PINNED. V08 installed `redis` unpinned, which
# silently pulled redis-py 8.x, whose automatic retries turned a 10 s BRPOP
# timeout into a 58 s freeze. Unpinned builds also let PC 1 and PC 2 end up on
# DIFFERENT library versions if their images were built on different days.
# These are the exact versions V09 was validated against.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --timeout 180 --retries 10 redis==8.1.0
RUN pip install --no-cache-dir --timeout 180 --retries 10 numpy==2.4.4
RUN pip install --no-cache-dir --timeout 180 --retries 10 scipy==1.17.1

WORKDIR /app
COPY *.py /app/

CMD ["python", "-c", "print('specify a command in docker-compose.yml')"]
