# Pocketful stage 4

Build and run:

```powershell
docker build -t pocketful-stage-4 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-4
```

The service listens on `0.0.0.0:$PORT`, defaults to 8080, and stores state in memory.
