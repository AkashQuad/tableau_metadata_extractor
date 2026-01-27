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

app = FastAPI(title="Tableau to Power BI Migrator - Metadata Extractor")

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
    'pie': 'Pie Chart', 'map': 'Map', 'filledmap': 'Map', 'shape': 'Shape',
    'ganttbar': 'Gantt'
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
    
    # Clean up ops for readability
    if op == "=": op = "=="
    
    if len(children) == 2:
        left = parse_join_expression(children[0])
        right = parse_join_expression(children[1])
        return f"({left} {op} {right})"
    
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
        "parameters": [],
        "dataSources": [],
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
        # A. PARAMETERS (Global Variables)
        # ==========================================
        for ds_node in root.findall(".//datasource[@name='Parameters']"):
            for col in ds_node.findall("column"):
                final_output["parameters"].append({
                    "name": clean_name(col.get("caption") or col.get("name")),
                    "dataType": col.get("datatype"),
                    "value": col.get("value")
                })

        # ==========================================
        # B. DATA SOURCES (Tables, Joins, Fields)
        # ==========================================
        for ds_node in root.findall(".//datasource"):
            ds_name = ds_node.get("caption") or ds_node.get("name")
            if ds_name == "Parameters": continue # Handled above

            ds_meta = {
                "name": ds_name,
                "tables": [],
                "relationships": [],
                "fields": {
                    "measures": [],
                    "calculatedColumns": [],
                    "dimensions": []
                },
                "hierarchies": []
            }

            # --- 1. TABLES (Deduplicated) ---
            # Tableau puts <relation> tags in <connection> AND <object-graph>. We want unique tables.
            seen_tables = set()
            
            # Helper to add table
            def add_table(rel_node):
                t_name = rel_node.get("table") or rel_node.get("name")
                t_type = rel_node.get("type")
                if t_name and t_type == "table":
                    clean_tbl = clean_name(t_name)
                    if clean_tbl not in seen_tables:
                        seen_tables.add(clean_tbl)
                        ds_meta["tables"].append({
                            "name": clean_tbl,
                            "rawName": t_name,
                            "connection": rel_node.get("connection")
                        })

            # Check Connection (Physical Layer)
            for conn in ds_node.findall(".//connection"):
                for rel in conn.findall(".//relation"):
                    add_table(rel)
            
            # Check Object Graph (Logical Layer) - Fallback if physical missing
            if not ds_meta["tables"]:
                for rel in ds_node.findall(".//relation"):
                    add_table(rel)

            # --- 2. RELATIONSHIPS ---
            # Strategy: Check Object Graph (Noodles) FIRST. If empty, check Relations (Joins).
            obj_graph = ds_node.find(".//object-graph")
            
            if obj_graph:
                # Modern "Noodle" Relationships
                id_map = {}
                for obj in obj_graph.findall(".//object"):
                    id_map[obj.get("id")] = clean_name(obj.get("caption"))

                for rel in obj_graph.findall(".//relationship"):
                    left = id_map.get(rel.find("first-end-point").get("object-id"))
                    right = id_map.get(rel.find("second-end-point").get("object-id"))
                    condition = parse_join_expression(rel.find("expression"))
                    
                    ds_meta["relationships"].append({
                        "type": "Logical",
                        "fromTable": left,
                        "toTable": right,
                        "joinCondition": condition
                    })
            else:
                # Legacy "Physical" Joins
                for join in ds_node.findall(".//relation[@type='join']"):
                    # This is harder to parse as it relies on nested structures. 
                    # Simplified extraction:
                    clause = join.find("clause")
                    expr = clause.get("expression") if clause is not None else "Unknown"
                    ds_meta["relationships"].append({
                        "type": "Join (" + join.get("join", "") + ")",
                        "joinCondition": expr
                    })

            # --- 3. FIELDS (Dimensions, Measures, DAX) ---
            for col in ds_node.findall("column"):
                name = col.get("name")
                if not name or "tableau_internal_object_id" in name: continue

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

                if formula:
                    if role == "measure":
                        field_def["category"] = "Measure"
                        ds_meta["fields"]["measures"].append(field_def)
                    else:
                        field_def["category"] = "Calculated Column"
                        ds_meta["fields"]["calculatedColumns"].append(field_def)
                elif role == "measure":
                    field_def["isImplicit"] = True
                    ds_meta["fields"]["measures"].append(field_def)
                else:
                    ds_meta["fields"]["dimensions"].append(field_def)

            # --- 4. HIERARCHIES (Drill Paths) ---
            for drill in ds_node.findall("drill-paths/drill-path"):
                h_name = drill.get("name")
                levels = []
                for field in drill.findall("field"):
                    levels.append(clean_name(field.get("name")))
                
                ds_meta["hierarchies"].append({
                    "name": h_name,
                    "levels": levels
                })

            final_output["dataSources"].append(ds_meta)

        # ==========================================
        # C. WORKSHEETS (Visuals)
        # ==========================================
        for ws in root.findall(".//worksheet"):
            sheet_name = ws.get('name')
            
            # Columns Used
            columns = set()
            for dep in ws.findall(".//datasource-dependencies"):
                for col_inst in dep.findall("column-instance"):
                    c_name = col_inst.get("column") or col_inst.get("name")
                    if c_name: columns.add(clean_name(c_name))

            # Visual Type Detection
            v_type = "Automatic"
            for mark in ws.findall(".//pane/mark"):
                cls = mark.get("class")
                if cls and cls != "Automatic":
                    v_type = MARK_MAP.get(cls.lower(), cls)
            
            final_output["worksheets"].append({
                "name": sheet_name,
                "visualType": v_type,
                "usedColumns": list(columns)
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
            
            # Smart naming for output file
            base_name = unquote(os.path.basename(payload.inputBlobUrl))
            output_name = os.path.splitext(base_name)[0] + "_full_metadata.json"
            
            url = upload_json_to_blob(payload.outputContainerUrl, output_name, data)
            
            return {
                "status": "success", 
                "outputBlobUrl": url,
                "summary": {
                    "dataSources": len(data["dataSources"]),
                    "worksheets": len(data["worksheets"]),
                    "parameters": len(data["parameters"])
                }
            }
    except Exception as e:
        raise HTTPException(500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
