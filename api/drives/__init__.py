import base64
import datetime
import json
import logging
import os
import uuid

import azure.functions as func
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

DB_NAME = "trackerdb"
CONTAINER_NAME = "drives"
POSTER_CONTAINER = "posters"
ALLOWED_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

_cosmos_container = None
_blob_service = None


def get_container():
    global _cosmos_container
    if _cosmos_container is None:
        client = CosmosClient.from_connection_string(os.environ["COSMOS_CONNECTION"])
        _cosmos_container = client.get_database_client(DB_NAME).get_container_client(CONTAINER_NAME)
    return _cosmos_container


def get_blob_service():
    global _blob_service
    if _blob_service is None:
        _blob_service = BlobServiceClient.from_connection_string(os.environ["STORAGE_CONNECTION"])
        try:
            _blob_service.create_container(POSTER_CONTAINER)
        except ResourceExistsError:
            pass
    return _blob_service


def reply(body, status=200):
    return func.HttpResponse(json.dumps(body), status_code=status, mimetype="application/json")


def poster_url(blob_name):
    """Short-lived read-only link, so the container can stay private."""
    if not blob_name:
        return None
    svc = get_blob_service()
    sas = generate_blob_sas(
        account_name=svc.account_name,
        container_name=POSTER_CONTAINER,
        blob_name=blob_name,
        account_key=svc.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    )
    return f"https://{svc.account_name}.blob.core.windows.net/{POSTER_CONTAINER}/{blob_name}?{sas}"


def save_poster(drive_id, body):
    """Upload the poster if the request carries one. Returns blob name or None."""
    b64 = body.get("posterBase64")
    ctype = body.get("posterType")
    if not b64:
        return None
    if ctype not in ALLOWED_TYPES:
        raise ValueError("Poster must be PNG, JPG or WEBP")
    data = base64.b64decode(b64)
    if len(data) > 1_000_000:
        raise ValueError("Poster must be under 1 MB")
    blob_name = f"{drive_id}.{ALLOWED_TYPES[ctype]}"
    get_blob_service().get_blob_client(POSTER_CONTAINER, blob_name).upload_blob(
        data, overwrite=True, content_settings=ContentSettings(content_type=ctype)
    )
    return blob_name


def delete_poster(blob_name):
    if not blob_name:
        return
    try:
        get_blob_service().get_blob_client(POSTER_CONTAINER, blob_name).delete_blob()
    except ResourceNotFoundError:
        pass


def public_view(item):
    return {
        "id": item["id"],
        "company": item.get("company", ""),
        "role": item.get("role", ""),
        "date": item.get("date", ""),
        "status": item.get("status", "Upcoming"),
        "posterUrl": poster_url(item.get("poster")),
    }


def main(req: func.HttpRequest) -> func.HttpResponse:
    method = req.method.upper()
    drive_id = req.route_params.get("id")
    try:
        container = get_container()

        if method == "GET":
            items = list(container.query_items(
                query="SELECT * FROM c", enable_cross_partition_query=True))
            items.sort(key=lambda i: i.get("date", ""))
            return reply([public_view(i) for i in items])

        if method == "POST":
            body = req.get_json()
            if not body.get("company") or not body.get("role"):
                return reply({"error": "company and role are required"}, 400)
            new_id = str(uuid.uuid4())
            item = {
                "id": new_id,
                "company": body["company"].strip(),
                "role": body["role"].strip(),
                "date": body.get("date", ""),
                "status": body.get("status", "Upcoming"),
                "poster": save_poster(new_id, body),
            }
            container.create_item(item)
            return reply(public_view(item), 201)

        if method == "PUT":
            if not drive_id:
                return reply({"error": "id missing in URL"}, 400)
            body = req.get_json()
            item = container.read_item(item=drive_id, partition_key=drive_id)
            for field in ("company", "role", "date", "status"):
                if field in body:
                    item[field] = body[field]
            new_poster = save_poster(drive_id, body)
            if new_poster:
                if item.get("poster") and item["poster"] != new_poster:
                    delete_poster(item["poster"])
                item["poster"] = new_poster
            container.replace_item(item=drive_id, body=item)
            return reply(public_view(item))

        if method == "DELETE":
            if not drive_id:
                return reply({"error": "id missing in URL"}, 400)
            item = container.read_item(item=drive_id, partition_key=drive_id)
            delete_poster(item.get("poster"))
            container.delete_item(item=drive_id, partition_key=drive_id)
            return reply({"deleted": drive_id})

        return reply({"error": "method not allowed"}, 405)

    except CosmosResourceNotFoundError:
        return reply({"error": "drive not found"}, 404)
    except ValueError as e:
        return reply({"error": str(e)}, 400)
    except Exception as e:  # shows up in Application Insights / logs
        logging.exception("Unhandled error")
        return reply({"error": str(e)}, 500)
