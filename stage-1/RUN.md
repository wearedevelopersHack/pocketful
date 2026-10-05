# Stage 1 Pocketful Service

Build and run:

```sh
docker build -t pocketful-stage-1 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-1
```

Health check:

```sh
curl http://127.0.0.1:8080/health
```

The service is a standalone HTTP API with in-memory state. It supports the required
test reset/export/import endpoints and does not need outbound runtime networking.
