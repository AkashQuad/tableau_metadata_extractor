from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json
import os
import zipfile
import tempfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from azure.storage.blob import BlobClient
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Tableau Metadata Extractor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # allow all origins
    allow_credentials=True,
    allow_methods=["*"],          # allow all HTTP methods
    allow_headers=["*"],          # allow all headers
)

# -------------------------------------------------
# REQUEST MODEL
# -------------------------------------------------
class ExtractMetadataRequest(BaseModel):
    inputBlobUrl: str
    outputContainerUrl: str


# -------------------------------------------------
# DOWNLOAD FROM AZURE BLOB
# -------------------------------------------------
def download_blob_to_file(blob_url: str, local_path: str):
    blob = BlobClient.from_blob_url(blob_url)
    with open(local_path, "wb") as f:
        f.write(blob.download_blob().readall())


# -------------------------------------------------
# UPLOAD JSON TO AZURE BLOB
# -------------------------------------------------
def upload_json_to_blob(container_url: str, blob_name: str, data: dict) -> str:
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

    if not conn_str:
        raise Exception("AZURE_STORAGE_CONNECTION_STRING environment variable not set")

    container_name = container_url.rstrip("/").split("/")[-1]

    blob = BlobClient.from_connection_string(
        conn_str=conn_str,
        container_name=container_name,
        blob_name=blob_name
    )

    blob.upload_blob(
        json.dumps(data, indent=2),
        overwrite=True,
        content_type="application/json"
    )

    return blob.url


# -------------------------------------------------
# TABLEAU METADATA EXTRACTION
# -------------------------------------------------
def extract_tableau_metadata(twbx_path: str) -> dict:
    metadata = {
        "dataSource": {},
        "calculatedFields": [],
        "worksheets": [],
        "dashboards": [],
        "globalFilters": []
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(twbx_path, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

        # Find .twb
        twb_file = None
        for root, _, files in os.walk(tmpdir):
            for file in files:
                if file.endswith(".twb"):
                    twb_file = os.path.join(root, file)
                    break

        if not twb_file:
            raise Exception("No .twb file found inside TWBX")

        tree = ET.parse(twb_file)
        root = tree.getroot()

        # Datasource
        datasource = root.find(".//datasource")
        if datasource is not None:
            tables = []

            for relation in datasource.findall(".//relation"):
                table_name = relation.get("table")
                if not table_name:
                    continue

                columns = []
                for col in datasource.findall(".//column"):
                    if col.get("name") and col.get("datatype"):
                        columns.append({
                            "name": col.get("name").replace("[", "").replace("]", ""),
                            "dataType": col.get("datatype")
                        })

                tables.append({
                    "tableName": table_name,
                    "columns": columns
                })

            metadata["dataSource"] = {
                "name": datasource.get("name"),
                "type": "extract",
                "tables": tables
            }

        # Calculated fields
        for col in root.findall(".//column"):
            calc = col.find("calculation")
            if calc is not None:
                metadata["calculatedFields"].append({
                    "name": col.get("name").replace("[", "").replace("]", ""),
                    "expression": calc.get("formula")
                })

        # Worksheets
        for worksheet in root.findall(".//worksheet"):
            cols = []
            for c in worksheet.findall(".//column"):
                if c.get("name"):
                    cols.append({
                        "table": "unknown",
                        "column": c.get("name").replace("[", "").replace("]", "")
                    })

            metadata["worksheets"].append({
                "name": worksheet.get("name"),
                "visualType": "unknown",
                "columns": cols
            })

        # Dashboards
        for dashboard in root.findall(".//dashboard"):
            ws_names = []
            for zone in dashboard.findall(".//zone"):
                if zone.get("name"):
                    ws_names.append(zone.get("name"))

            metadata["dashboards"].append({
                "dashboardName": dashboard.get("name"),
                "worksheets": list(set(ws_names))
            })

    return metadata


# -------------------------------------------------
# API ENDPOINT
# -------------------------------------------------
@app.post("/extract-metadata")
def extract_metadata(payload: ExtractMetadataRequest):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_twbx = os.path.join(tmpdir, "input.twbx")

            download_blob_to_file(payload.inputBlobUrl, local_twbx)

            metadata = extract_tableau_metadata(local_twbx)

            base_name = unquote(os.path.basename(payload.inputBlobUrl))
            output_name = os.path.splitext(base_name)[0] + "_metadata.json"

            output_url = upload_json_to_blob(
                payload.outputContainerUrl,
                output_name,
                metadata
            )

        return {
            "status": "success",
            "outputBlobUrl": output_url
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
