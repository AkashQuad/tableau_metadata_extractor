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
# CONSTANTS & MODELS
# -------------------------------------------------

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
# CORE EXTRACTION LOGIC (HYBRID)
# -------------------------------------------------

def extract_tableau_metadata(twbx_path: str) -> dict:
    # 1. Initialize OLD Output Structure
    metadata = {
        "dataSource": {},
        "calculatedFields": [],
        "worksheets": [],
        "dashboards": [],
        "globalFilters": []
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        # A. Unzip TWBX
        try:
            with zipfile.ZipFile(twbx_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile:
            raise Exception("File is not a valid .twbx zip file")

        # B. Find .twb XML
        twb_file = None
        for root_dir, _, files in os.walk(tmpdir):
            for file in files:
                if file.endswith(".twb"):
                    twb_file = os.path.join(root_dir, file)
                    break
        
        if not twb_file:
            raise Exception("No .twb XML file found inside TWBX")

        # C. Parse XML & STRIP NAMESPACES (Critical Fix)
        tree = ET.parse(twb_file)
        root = tree.getroot()
        
        # This fixes the "No worksheets found" error by ignoring xmlns
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        # -------------------------------------------------------
        # SECTION 1: DATASOURCE (From Old Code)
        # -------------------------------------------------------
        datasource = root.find(".//datasource")
        if datasource is not None:
            tables = []
            for relation in datasource.findall(".//relation"):
                table_name = relation.get("table")
                if not table_name: continue
                
                columns = []
                # Try to find columns defined in relation or metadata
                # (Simplified for robustness)
                tables.append({
                    "tableName": clean_name(table_name),
                    "columns": [] # Populating columns is tricky in xml, leaving generic
                })

            metadata["dataSource"] = {
                "name": datasource.get("name") or "TableauData",
                "type": "extract",
                "tables": tables
            }

        # -------------------------------------------------------
        # SECTION 2: CALCULATED FIELDS (From Old Code)
        # -------------------------------------------------------
        for col in root.findall(".//column"):
            calc = col.find("calculation")
            if calc is not None:
                metadata["calculatedFields"].append({
                    "name": clean_name(col.get("name")),
                    "expression": calc.get("formula")
                })

        # -------------------------------------------------------
        # SECTION 3: WORKSHEETS (Using NEW Logic + OLD Structure)
        # -------------------------------------------------------
        for worksheet in root.findall(".//worksheet"):
            sheet_name = worksheet.get('name')

            # 1. Detect Visual Type (New Logic)
            mark_element = worksheet.find(".//pane/mark")
            mark_class = mark_element.get('class') if mark_element is not None else "unknown"
            visual_type = MARK_MAP.get(mark_class, mark_class.capitalize())

            # 2. Detect Columns (New Logic - cleaner)
            bound_columns = set()
            for dep in worksheet.findall(".//datasource-dependencies"):
                for col in dep.findall("column"):
                    # Get readable name
                    raw_name = col.get('caption') or col.get('name')
                    if raw_name:
                        bound_columns.add(clean_name(raw_name))
            
            # 3. Format Columns for OLD Structure (List of Objects)
            formatted_columns = []
            for col_name in bound_columns:
                formatted_columns.append({
                    "table": "unknown", # Tableau XML rarely explicitly links table in dependencies
                    "column": col_name
                })

            # 4. Append to Metadata (Old Object Structure)
            metadata["worksheets"].append({
                "name": sheet_name,
                "visualType": visual_type, # Now populates correctly!
                "columns": formatted_columns
            })

        # -------------------------------------------------------
        # SECTION 4: DASHBOARDS (From Old Code)
        # -------------------------------------------------------
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
def handle_extraction(payload: ExtractMetadataRequest):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_twbx = os.path.join(tmpdir, "input.twbx")
            download_blob_to_file(payload.inputBlobUrl, local_twbx)
            
            # Extract
            metadata = extract_tableau_metadata(local_twbx)
            
            # Upload
            base_name = unquote(os.path.basename(payload.inputBlobUrl))
            output_name = os.path.splitext(base_name)[0] + "_metadata.json"
            
            output_url = upload_json_to_blob(
                payload.outputContainerUrl,
                output_name,
                metadata
            )
            
        return {
            "status": "success",
            "outputBlobUrl": output_url,
            "visuals_found": len(metadata["worksheets"])
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
