from fastapi import FastAPI

from apps.api.app.api.routes import router

app = FastAPI(title="Tally API", version="0.1.0")
app.include_router(router)
