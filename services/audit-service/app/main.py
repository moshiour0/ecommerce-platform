from fastapi import FastAPI

app = FastAPI(title="Audit Service")

@app.get("/health")
def health_check():
    return {"status": "ok"}
