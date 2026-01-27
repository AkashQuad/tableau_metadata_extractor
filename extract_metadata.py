import json
import os
import zipfile
import tempfile
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from azure.storage.blob import BlobClient
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Tableau Metadata Extractor (Multi-DS)")

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
    'bar': 'Bar Chart', 'line': 'Line Chart', 'area': 'Area Chart',
    'text': 'Text Table', 'circle': 'Scatter Plot', 'square': 'Heat Map',
    'pie': 'Pie Chart', 'map': 'Map', 'filledmap': 'Map'
}

class ExtractMetadataRequest(BaseModel):
    inputBlobUrl: str
    outputContainerUrl: str

# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def clean_name(name: str):
    """Removes brackets and internal Tableau prefixes/suffixes."""
    if not name: return None
    name = name.replace("[", "").replace("]", "")
    # Remove aggregate prefixes and internal suffixes
    name = re.sub(r'^(none|sum|avg|min|max|count|attr|yr|mn|dy|qd|tdc):', '', name, flags=re.IGNORECASE)
    name = re.sub(r':(nk|ok|qk|sk)$', '', name, flags=re.IGNORECASE)
    return name.strip()

def parse_join_expression(node):
    """Recursively parses XML join trees into string logic."""
    if node is None: return ""
    op = node.get("op")
    children = list(node)
    if not children: return op
    if len(children) == 2:
        left = parse_join_expression(children[0])
        right = parse_join_expression(children[1])
        return f"{left} {op} {right}"
    return op

def download_blob_to_file(blob_url: str, local_path: str):
    blob = BlobClient.from_blob_url(blob_url)
    with open(local_path, "wb") as f:
        f.write(blob.download_blob().readall())

def upload_json_to_blob(container_url: str, blob_name: str, data: dict):
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        print("WARNING: Azure Storage Connection String missing. Skipping upload.")
        return "local-mode"
    container_name = container_url.rstrip("/").split("/")[-1]
    blob = BlobClient.from_connection_string(conn_str, container_name, blob_name)
    blob.upload_blob(json.dumps(data, indent=2), overwrite=True, content_type="application/json")
    return blob.url

# -------------------------------------------------
# CORE LOGIC
# -------------------------------------------------

def extract_tableau_metadata(twbx_path: str) -> dict:
    final_output = {
        "dataSources": [],  # Changed to list to handle multiple DS
        "worksheets": []
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Unzip
        try:
            with zipfile.ZipFile(twbx_path, "r") as z:
                z.extractall(tmpdir)
        except zipfile.BadZipFile:
            raise Exception("Invalid TWBX file")

        # 2. Find .twb
        twb_path = next((os.path.join(r, f) for r, _, fs in os.walk(tmpdir) for f in fs if f.endswith(".twb")), None)
        if not twb_path: raise Exception("No .twb found")

        # 3. Parse XML (Strip Namespaces)
        tree = ET.parse(twb_path)
        root = tree.getroot()
        for elem in root.iter():
            if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]

        # ==========================================
        # LOOP THROUGH ALL DATA SOURCES
        # ==========================================
        # In your XML, you have multiple <datasource> tags. We must process each.
        for ds_node in root.findall(".//datasource"):
            ds_name = ds_node.get("caption") or ds_node.get("name")
            
            # Skip "Parameters" datasource if it exists (Tableau specific)
            if ds_name == "Parameters": continue

            ds_meta = {
                "name": ds_name,
                "tables": [],
                "relationships": [],
                "fields": {
                    "measures": [],
                    "calculatedColumns": [],
                    "dimensions": []
                }
            }

            # --- A. TABLES ---
            table_map = {} # Map ID -> Clean Name
            
            # Find physical tables (connection references)
            for rel in ds_node.findall(".//relation"):
                t_name = rel.get("table") or rel.get("name")
                t_type = rel.get("type")
                
                # Check if it's a valid table (and not a nested join clause)
                if t_name and t_type == "table":
                    clean_tbl = clean_name(t_name)
                    # Deduplication check
                    if not any(t['name'] == clean_tbl for t in ds_meta["tables"]):
                        ds_meta["tables"].append({
                            "name": clean_tbl,
                            "rawName": t_name,
                            "connection": rel.get("connection")
                        })

            # --- B. RELATIONSHIPS (Noodles) ---
            obj_graph = ds_node.find(".//object-graph")
            if obj_graph:
                # 1. Build ID Map
                for obj in obj_graph.findall(".//object"):
                    obj_id = obj.get("id")
                    caption = obj.get("caption")
                    table_map[obj_id] = clean_name(caption)
                
                # 2. Extract Relationships
                for rel in obj_graph.findall(".//relationship"):
                    left_id = rel.find("first-end-point").get("object-id")
                    right_id = rel.find("second-end-point").get("object-id")
                    
                    join_expr = parse_join_expression(rel.find("expression"))

                    ds_meta["relationships"].append({
                        "fromTable": table_map.get(left_id, left_id),
                        "toTable": table_map.get(right_id, right_id),
                        "joinCondition": join_expr
                    })

            # --- C. FIELDS (DAX Prep) ---
            for col in ds_node.findall("column"):
                name = col.get("name")
                # Skip internal Tableau calculation artifacts
                if "tableau_internal_object_id" in name: continue

                caption = col.get("caption") or clean_name(name)
                role = col.get("role")
                datatype = col.get("datatype")
                
                calc = col.find("calculation")
                formula = calc.get("formula") if calc is not None else None

                field_def = {
                    "name": caption,
                    "rawName": name,
                    "dataType": datatype,
                    "formula": formula
                }

                # CLASSIFICATION LOGIC
                if formula:
                    if role == "measure":
                        ds_meta["fields"]["measures"].append(field_def)
                    else:
                        ds_meta["fields"]["calculatedColumns"].append(field_def)
                elif role == "measure":
                    field_def["isImplicit"] = True
                    ds_meta["fields"]["measures"].append(field_def)
                else:
                    ds_meta["fields"]["dimensions"].append(field_def)

            final_output["dataSources"].append(ds_meta)

        # ==========================================
        # WORKSHEETS
        # ==========================================
        for ws in root.findall(".//worksheet"):
            sheet_name = ws.get('name')
            
            columns = set()
            for dep in ws.findall(".//datasource-dependencies"):
                for col_inst in dep.findall("column-instance"):
                    c_name = col_inst.get("column") or col_inst.get("name")
                    if c_name: columns.add(clean_name(c_name))

            v_type = "Automatic"
            for mark in ws.findall(".//pane/mark"):
                cls = mark.get("class")
                if cls and cls != "Automatic":
                    v_type = MARK_MAP.get(cls.lower(), cls)
            
            final_output["worksheets"].append({
                "name": sheet_name,
                "type": v_type,
                "columns": list(columns)
            })

    return final_output

# -------------------------------------------------
# ENDPOINT
# -------------------------------------------------

@app.post("/extract-metadata")
def api_handler(payload: ExtractMetadataRequest):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = os.path.join(tmpdir, "input.twbx")
            download_blob_to_file(payload.inputBlobUrl, local_path)
            
            data = extract_tableau_metadata(local_path)
            
            output_name = os.path.splitext(os.path.basename(payload.inputBlobUrl))[0] + "_v3_multi_metadata.json"
            url = upload_json_to_blob(payload.outputContainerUrl, output_name, data)
            
            # Stats for response
            ds_count = len(data["dataSources"])
            total_tables = sum(len(d["tables"]) for d in data["dataSources"])
            
            return {
                "status": "success", 
                "outputUrl": url,
                "stats": {
                    "dataSources": ds_count,
                    "totalTables": total_tables,
                    "worksheets": len(data["worksheets"])
                }
            }
    except Exception as e:
        raise HTTPException(500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
