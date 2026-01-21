import json

import os

import zipfile

import tempfile

import xml.etree.ElementTree as ET

from urllib.parse import unquote
 
from fastapi import FastAPI, HTTPException

from pydantic import BaseModel

from azure.storage.blob import BlobClient

from fastapi.middleware.cors import CORSMiddleware
 
app = FastAPI(title="Tableau Metadata Extractor API")
 
app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"],

)
 
# -------------------------------------------------

# REQUEST MODEL

# -------------------------------------------------

class ExtractMetadataRequest(BaseModel):

    inputBlobUrl: str

    outputContainerUrl: str
 
# -------------------------------------------------

# HELPERS

# -------------------------------------------------

def clean_name(name: str):

    """Removes Tableau's internal brackets from field names."""

    if not name:

        return name

    return name.replace("[", "").replace("]", "")
 
def download_blob_to_file(blob_url: str, local_path: str):

    blob = BlobClient.from_blob_url(blob_url)

    with open(local_path, "wb") as f:

        f.write(blob.download_blob().readall())
 
def upload_json_to_blob(container_url: str, blob_name: str, data: list) -> str:

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

# CORE EXTRACTION LOGIC

# -------------------------------------------------

def extract_tableau_metadata(twbx_path: str) -> list:

    # Map of Tableau internal mark classes to readable visual types

    MARK_MAP = {

        'bar': 'Bar Chart',

        'line': 'Line Chart',

        'area': 'Area Chart',

        'text': 'Text Table / Crosstab',

        'circle': 'Scatter Plot',

        'square': 'Heat Map',

        'pie': 'Pie Chart',

        'map': 'Map',

        'ganttbar': 'Gantt Chart',

        'shape': 'Shape Chart'

    }
 
    extraction_results = []
 
    with tempfile.TemporaryDirectory() as tmpdir:

        # Step 1: Handle TWBX (it's a zip file)

        try:

            with zipfile.ZipFile(twbx_path, "r") as zip_ref:

                zip_ref.extractall(tmpdir)

        except zipfile.BadZipFile:

            raise Exception("The file provided is not a valid .twbx (Zip) file.")
 
        # Step 2: Find the .twb file inside the extracted contents

        twb_file = None

        for root_dir, _, files in os.walk(tmpdir):

            for file in files:

                if file.endswith(".twb"):

                    twb_file = os.path.join(root_dir, file)

                    break

        if not twb_file:

            raise Exception("No .twb XML file found inside the TWBX package.")
 
        # Step 3: Parse XML

        tree = ET.parse(twb_file)

        root = tree.getroot()
 
        # Step 4: Iterate through each worksheet (Visual)

        for worksheet in root.findall(".//worksheet"):

            sheet_name = worksheet.get('name')

            # Identify Visual Type (Mark Type)

            # Located in <pane><mark class="..."/></pane>

            mark_element = worksheet.find(".//pane/mark")

            mark_class = mark_element.get('class') if mark_element is not None else "unknown"

            visual_type = MARK_MAP.get(mark_class, mark_class.capitalize())
 
            # Identify Bound Columns

            # We look for columns referenced specifically in this worksheet's dependencies

            bound_columns = set()

            # datasource-dependencies contains all fields used in the sheet (rows, cols, marks, filters)

            for dep in worksheet.findall(".//datasource-dependencies"):

                for col in dep.findall("column"):

                    # Use 'caption' (User friendly) if available, else 'name' (technical)

                    raw_name = col.get('caption') or col.get('name')

                    if raw_name:

                        bound_columns.add(clean_name(raw_name))
 
            extraction_results.append({

                "worksheet_name": sheet_name,

                "visual_type": visual_type,

                "bound_columns": sorted(list(bound_columns))

            })
 
    return extraction_results
 
# -------------------------------------------------

# API ENDPOINT

# -------------------------------------------------

@app.post("/extract-metadata")

def handle_extraction(payload: ExtractMetadataRequest):

    try:

        with tempfile.TemporaryDirectory() as tmpdir:

            # 1. Download input file

            local_twbx = os.path.join(tmpdir, "input.twbx")

            download_blob_to_file(payload.inputBlobUrl, local_twbx)
 
            # 2. Extract Data

            metadata_list = extract_tableau_metadata(local_twbx)
 
            # 3. Prepare Output Name (original_filename_metadata.json)

            base_name = unquote(os.path.basename(payload.inputBlobUrl))

            output_name = os.path.splitext(base_name)[0] + "_metadata.json"
 
            # 4. Upload Result back to Azure

            output_url = upload_json_to_blob(

                payload.outputContainerUrl,

                output_name,

                metadata_list

            )
 
        return {

            "status": "success",

            "message": f"Metadata extracted from {len(metadata_list)} visuals",

            "outputBlobUrl": output_url

        }
 
    except Exception as e:

        raise HTTPException(status_code=500, detail=str(e))
 
if __name__ == "__main__":

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
 
