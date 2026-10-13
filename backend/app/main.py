from fastapi import FastAPI

app = FastAPI(
    title="Emergency Response Intelligence Platform",
    description="AI-powered disaster management and emergency response platform.",
    version="0.1.0",
)


@app.get("/")
def root():
    return {
        "message": "Emergency Response Intelligence Platform API",
        "status": "running",
        "version": "0.1.0",
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy"
    }