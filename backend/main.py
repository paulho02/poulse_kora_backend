from app.core.logger import get_logger
from app.factory import create_app

app = create_app()  # also installs the logging config, see app/core/logger.py

log = get_logger(__name__)

if __name__ == "__main__":
    import uvicorn

    log.info("uvicorn.starting", reload=True, port=8000)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        reload=True,
        port=int("8000"),
    )
