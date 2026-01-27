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
    'text': 'Text Table',
    'circle': 'Scatter Plot',
    'square': 'Heat Map',
    'pie': 'Pie Chart',
    'map': 'Map',
    'ganttbar': 'Gantt Chart',
    'shape': 'Shape Chart',
    'scatter': 'Scatter Plot',
    'multipolygon': 'Map',
    'filledmap': 'Map'
}

class ExtractMetadataRequest(BaseModel):
    inputBlobUrl: str
    outputContainerUrl: str

# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def clean_name(name: str):
    """
    Cleans Tableau field names.
    """
    if not name:
        return name
    
    # Remove brackets
    name = name.replace("[", "").replace("]", "")
    
    # Remove internal patterns
    name = re.sub(r'^(none|sum|avg|min|max|count|attr|yr|mn|dy|qd|tdc):', '', name, flags=re.IGNORECASE)
    name = re.sub(r':(nk|ok|qk|sk)$', '', name, flags=re.IGNORECASE)
    
    return name

def is_aggregation(formula: str) -> bool:
    """
    Simple heuristic to check if a formula is likely a Measure (Aggregation) 
    vs a Calculated Column (Row Level).
    """
    if not formula:
        return False
    aggs = ['SUM(', 'AVG(', 'COUNT(', 'COUNTD(', 'MIN(', 'MAX(', 'ATTR(']
    formula_upper = formula.upper()
    return any(agg in formula_upper for agg in aggs)

def download_blob_to_file(blob_url: str, local_path: str):
    blob = BlobClient.from_blob_url(blob_url)
    with open(local_path, "wb") as f:
        f.write(blob.download_blob().readall())

def upload_json_to_blob(container_url: str, blob_name: str, data: dict) -> str:
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        # Fallback for local testing if env var is missing, prints warning
        print("WARNING: AZURE_STORAGE_CONNECTION_STRING not set. Skipping upload.")
        return "local-test-no-upload"

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

def extract_tableau_metadata(twbx_path: str) -> dict:
    metadata = {
        "dataSource": {},
        "relationships": [],    # NEW: Captures Joins/Relationships
        "measures": [],         # NEW: Captures Aggregations (DAX candidates)
        "calculatedColumns": [],# NEW: Captures Row-level calcs
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

        # C. Parse XML & STRIP NAMESPACES
        tree = ET.parse(twb_file)
        root = tree.getroot()
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        # -------------------------------------------------------
        # SECTION 1: DATASOURCE & TABLES
        # -------------------------------------------------------
        datasource = root.find(".//datasource")
        if datasource is not None:
            tables = []
            
            # Simple Table Extraction
            for relation in datasource.findall(".//relation"):
                table_name = relation.get("table")
                if not table_name: continue
                
                # Cleanup table name (Tableau usually wraps in [Table])
                clean_tbl = clean_name(table_name)
                
                tables.append({
                    "tableName": clean_tbl,
                    "rawName": table_name,
                    "type": relation.get("type", "table") # table, join, text, etc.
                })

            metadata["dataSource"] = {
                "name": datasource.get("name") or "TableauData",
                "type": "extract",
                "tables": tables
            }

            # -------------------------------------------------------
            # SECTION 1.5: RELATIONSHIPS (JOINS) -- NEW FEATURE
            # -------------------------------------------------------
            # We look for <relation> tags that have type='join'
            for join_rel in datasource.findall(".//relation[@type='join']"):
                join_type = join_rel.get("join") # inner, left, etc.
                
                # Tableau XML nests the tables inside the join relation
                # Usually <relation type='join'> <clause> ... </clause> <relation name='L' ...> <relation name='R' ...> </relation>
                
                # This is a basic parser. Complex nested joins require recursive parsing.
                # We attempt to find the join clause expression.
                clause = join_rel.find("clause")
                expression = clause.get("expression") if clause is not None else ""
                
                metadata["relationships"].append({
                    "type": join_type,
                    "expression": expression, # "([Orders].[ID] = [Returns].[ID])"
                    "parsed": "Complex join - check expression" 
                })

        # -------------------------------------------------------
        # SECTION 2: CALCULATED FIELDS & MEASURES -- IMPROVED
        # -------------------------------------------------------
        for col in root.findall(".//column"):
            name = col.get("name")
            caption = col.get("caption") or clean_name(name)
            
            calc = col.find("calculation")
            if calc is not None:
                formula = calc.get("formula")
                if formula:
                    # Classify: Measure vs Column
                    obj = {
                        "name": caption,
                        "rawName": name,
                        "formula": formula,
                        "dataType": col.get("datatype", "unknown")
                    }
                    
                    if is_aggregation(formula):
                        obj["type"] = "Measure"
                        metadata["measures"].append(obj)
                    else:
                        obj["type"] = "CalculatedColumn"
                        metadata["calculatedColumns"].append(obj)

        # -------------------------------------------------------
        # SECTION 3: WORKSHEETS (Unchanged Logic)
        # -------------------------------------------------------
        for worksheet in root.findall(".//worksheet"):
            sheet_name = worksheet.get('name')
            bound_columns_set = set()
            
            # Detect Columns used
            for dep in worksheet.findall(".//datasource-dependencies"):
                for col in dep.findall("column-instance"):
                    col_ref = col.get('column')
                    clean_col = None
                    if col_ref:
                        parts = col_ref.split(']:')
                        if len(parts) > 1:
                            clean_col = clean_name(parts[-1])
                    if not clean_col: 
                        clean_col = clean_name(col.get('name'))
                    if clean_col:
                        bound_columns_set.add(clean_col)

            # Smart Visual Detection
            visual_type = "Automatic"
            for mark_element in worksheet.findall(".//pane/mark"):
                cls = mark_element.get('class')
                if cls and cls != "Automatic":
                    visual_type = MARK_MAP.get(cls.lower(), cls.capitalize())
                    break
            
            if visual_type == "Automatic":
                if worksheet.find(".//style-rule[@element='map']") is not None: visual_type = "Map"
                elif worksheet.find(".//style-rule[@element='table']") is not None: visual_type = "Text Table"
            
            if visual_type == "Automatic":
                col_list_lower = [c.lower() for c in bound_columns_set]
                if any(x in col for col in col_list_lower for x in ['lat', 'lon', 'geo']): visual_type = "Map"
                elif len(bound_columns_set) == 1: visual_type = "Text Table"
                else: visual_type = "Bar Chart"

            formatted_columns = [{"table": "MainTable", "column": col} for col in sorted(list(bound_columns_set))]

            metadata["worksheets"].append({
                "name": sheet_name,
                "visualType": visual_type, 
                "columns": formatted_columns
            })

        # -------------------------------------------------------
        # SECTION 4: DASHBOARDS
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
            "counts": {
                "visuals": len(metadata["worksheets"]),
                "measures": len(metadata["measures"]),
                "calc_columns": len(metadata["calculatedColumns"]),
                "relationships": len(metadata["relationships"])
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
