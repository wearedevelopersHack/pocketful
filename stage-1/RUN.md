# Pocketful stage 1

Build and run:

```powershell
docker build -t pocketful-stage-1 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-1
```

The service listens on `0.0.0.0:$PORT`, defaults to 8080, and stores state in memory.
