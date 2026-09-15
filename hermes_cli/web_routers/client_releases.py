"""Authenticated release policy and artifact delivery for remote Desktop clients."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from hermes_cli.client_release_policy import (
    ClientReleasePolicyError,
    artifact_file,
    evaluate_client_release,
)
from hermes_cli.web_deps import late
from hermes_cli.web_models import ClientReleaseIdentity


router = APIRouter()
load_config = late("load_config", "hermes_cli.config")


@router.post("/api/client-release/policy")
async def client_release_policy(identity: ClientReleaseIdentity):
    try:
        return evaluate_client_release(identity.model_dump(), load_config())
    except ClientReleasePolicyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/client-release/artifacts/{platform}/{arch}")
async def download_client_release(platform: str, arch: str):
    try:
        path = artifact_file(load_config(), platform, arch)
    except ClientReleasePolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
