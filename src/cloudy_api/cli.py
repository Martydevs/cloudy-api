import os


def main() -> None:
    """Entry point for the ``cloudy-api`` script."""
    import uvicorn

    uvicorn.run(
        "cloudy_api.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        workers=1,
    )
