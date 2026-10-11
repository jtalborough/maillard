"""FastAPI application for Maillard."""

from fastapi import FastAPI

from maillard.__version__ import VERSION

app = FastAPI(
    title="Maillard",
    description="AI-powered coffee roasting assistant for Kaleido M10",
    version=VERSION,
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy"}


# Routers will be added in Phase 3
