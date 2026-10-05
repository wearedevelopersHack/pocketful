# Pocketful stage 2

Build and run:

```powershell
docker build -t pocketful-stage-2 .
docker run --rm -e PORT=8080 -p 8080:8080 pocketful-stage-2
```

The service listens on `0.0.0.0:$PORT`, defaults to 8080, and stores state in memory.
