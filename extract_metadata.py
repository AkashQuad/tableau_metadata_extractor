# import json
# import os
# import zipfile
# import tempfile
# import re
# import xml.etree.ElementTree as ET
# from urllib.parse import unquote

# # Third-party imports
# from fastapi import FastAPI, HTTPException
# from fastapi.middleware.cors import CORSMiddleware
# from pydantic import BaseModel
# from azure.storage.blob import BlobClient

# # Initialize App
# app = FastAPI(title="Tableau Metadata Extractor API")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # -------------------------------------------------
# # CONSTANTS & MODELS
# # -------------------------------------------------

# MARK_MAP = {
#     'bar': 'Bar Chart',
#     'line': 'Line Chart',
#     'area': 'Area Chart',
#     'text': 'Text Table',
#     'circle': 'Scatter Plot',
#     'square': 'Heat Map',
#     'pie': 'Pie Chart',
#     'map': 'Map',
#     'ganttbar': 'Gantt Chart',
#     'shape': 'Shape Chart',
#     'scatter': 'Scatter Plot',
#     'multipolygon': 'Map',
#     'filledmap': 'Map'
# }

# class ExtractMetadataRequest(BaseModel):
#     inputBlobUrl: str
#     outputContainerUrl: str

# # -------------------------------------------------
# # HELPERS
# # -------------------------------------------------

# def clean_name(name: str) -> str:
#     """
#     Cleans Tableau field names.
#     """
#     if not name:
#         return ""
    
#     # 1. Remove brackets
#     name = name.replace("[", "").replace("]", "")
    
#     # 2. Remove Tableau internal patterns (start prefixes)
#     name = re.sub(r'^(none|sum|avg|min|max|count|attr|yr|mn|dy|qd|tdc):', '', name, flags=re.IGNORECASE)
    
#     # 3. Remove internal suffixes
#     name = re.sub(r':(nk|ok|qk|sk)$', '', name, flags=re.IGNORECASE)
    
#     return name

# def get_blob_client(blob_url: str):
#     """
#     Helper to get a BlobClient. 
#     Tries to use Connection String if available to handle Auth,
#     otherwise falls back to the URL (assuming SAS token exists).
#     """
#     conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    
#     # If we have a connection string, parse the blob name/container from the URL
#     # to ensure we use the authenticated client.
#     if conn_str:
#         try:
#             # Logic to parse container and blob name from URL if needed
#             # For simplicity, we assume if conn_str exists, we prefer it.
#             # However, mapping a full URL to a client via conn string requires parsing.
#             # If the URL is external (SAS), use from_blob_url.
#             return BlobClient.from_blob_url(blob_url) 
#         except Exception:
#             pass
            
#     # Fallback to URL (Must have SAS token if private)
#     return BlobClient.from_blob_url(blob_url)

# def download_blob_to_file(blob_url: str, local_path: str):
#     # NOTE: If your blob is private, blob_url MUST include a SAS token
#     # OR you must use a credential object here.
#     blob = BlobClient.from_blob_url(blob_url)
    
#     # If using Managed Identity or Connection String for the input too:
#     # blob = BlobClient.from_connection_string(conn_str, container, blob_name)
    
#     with open(local_path, "wb") as f:
#         data = blob.download_blob()
#         data.readinto(f)

# def upload_json_to_blob(container_url: str, blob_name: str, data: dict) -> str:
#     conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
#     if not conn_str:
#         raise ValueError("AZURE_STORAGE_CONNECTION_STRING environment variable not set")

#     # specific parsing to get container name roughly
#     # container_url input might be https://account.blob.core.windows.net/container
#     container_name = container_url.rstrip("/").split("/")[-1]
    
#     blob = BlobClient.from_connection_string(
#         conn_str=conn_str,
#         container_name=container_name,
#         blob_name=blob_name
#     )
#     blob.upload_blob(
#         json.dumps(data, indent=2),
#         overwrite=True,
#         content_type="application/json"
#     )
#     return blob.url

# # -------------------------------------------------
# # CORE EXTRACTION LOGIC
# # -------------------------------------------------

# def extract_tableau_metadata(twbx_path: str) -> dict:
#     metadata = {
#         "dataSource": {},
#         "calculatedFields": [],
#         "worksheets": [],
#         "dashboards": [],
#         "globalFilters": []
#     }

#     with tempfile.TemporaryDirectory() as tmpdir:
#         # A. Unzip TWBX
#         try:
#             with zipfile.ZipFile(twbx_path, "r") as zip_ref:
#                 zip_ref.extractall(tmpdir)
#         except zipfile.BadZipFile:
#             raise ValueError("File is not a valid .twbx zip file")

#         # B. Find .twb XML
#         twb_file = None
#         for root_dir, _, files in os.walk(tmpdir):
#             for file in files:
#                 if file.endswith(".twb"):
#                     twb_file = os.path.join(root_dir, file)
#                     break
        
#         if not twb_file:
#             raise ValueError("No .twb XML file found inside TWBX")

#         # C. Parse XML & STRIP NAMESPACES
#         try:
#             tree = ET.parse(twb_file)
#             root = tree.getroot()
#         except ET.ParseError:
#             raise ValueError("Failed to parse .twb XML content")
        
#         # Namespace stripping
#         for elem in root.iter():
#             if '}' in elem.tag:
#                 elem.tag = elem.tag.split('}', 1)[1]

#         # 1. DATASOURCE
#         datasource = root.find(".//datasource")
#         if datasource is not None:
#             tables = []
#             for relation in datasource.findall(".//relation"):
#                 table_name = relation.get("table")
#                 if not table_name: 
#                     continue
                
#                 tables.append({
#                     "tableName": clean_name(table_name),
#                     "columns": [] 
#                 })

#             metadata["dataSource"] = {
#                 "name": datasource.get("name") or "TableauData",
#                 "type": "extract",
#                 "tables": tables
#             }

#         # 2. CALCULATED FIELDS
#         for col in root.findall(".//column"):
#             calc = col.find("calculation")
#             if calc is not None:
#                 metadata["calculatedFields"].append({
#                     "name": clean_name(col.get("name")),
#                     "expression": calc.get("formula")
#                 })

#         # 3. WORKSHEETS
#         for worksheet in root.findall(".//worksheet"):
#             sheet_name = worksheet.get('name')
#             bound_columns_set = set()

#             # Dependency Detection
#             for dep in worksheet.findall(".//datasource-dependencies"):
#                 for col in dep.findall("column-instance"):
#                     col_ref = col.get('column')
#                     clean_col = None
                    
#                     if col_ref:
#                         # [some_table].[column_name] -> column_name
#                         parts = col_ref.split(']:')
#                         if len(parts) > 1:
#                             clean_col = clean_name(parts[-1])
                    
#                     if not clean_col: 
#                         clean_col = clean_name(col.get('name'))
                        
#                     if clean_col:
#                         bound_columns_set.add(clean_col)

#             # Smart Visual Detection
#             visual_type = "Automatic"
            
#             # A. Check Marks
#             for mark_element in worksheet.findall(".//pane/mark"):
#                 cls = mark_element.get('class')
#                 if cls and cls != "Automatic":
#                     visual_type = MARK_MAP.get(cls.lower(), cls.capitalize())
#                     break
            
#             # B. Check Style Rules
#             if visual_type == "Automatic":
#                 if worksheet.find(".//style-rule[@element='map']") is not None:
#                     visual_type = "Map"
#                 elif worksheet.find(".//style-rule[@element='table']") is not None:
#                     visual_type = "Text Table"
            
#             # C. Guess based on columns
#             if visual_type == "Automatic":
#                 col_list_lower = [c.lower() for c in bound_columns_set]
#                 map_keywords = ['lat', 'lon', 'country', 'city', 'state', 'zip', 'geo']
                
#                 if any(k in col for col in col_list_lower for k in map_keywords):
#                     visual_type = "Map"
#                 elif len(bound_columns_set) == 1:
#                     visual_type = "Text Table"
#                 else:
#                     visual_type = "Bar Chart"

#             formatted_columns = [
#                 {"table": "MainTable", "column": col} 
#                 for col in sorted(list(bound_columns_set))
#             ]

#             metadata["worksheets"].append({
#                 "name": sheet_name,
#                 "visualType": visual_type, 
#                 "columns": formatted_columns
#             })

#         # 4. DASHBOARDS
#         for dashboard in root.findall(".//dashboard"):
#             ws_names = []
#             for zone in dashboard.findall(".//zone"):
#                 z_name = zone.get("name")
#                 if z_name:
#                     ws_names.append(z_name)
            
#             metadata["dashboards"].append({
#                 "dashboardName": dashboard.get("name"),
#                 "worksheets": list(set(ws_names))
#             })

#     return metadata

# # -------------------------------------------------
# # API ENDPOINT
# # -------------------------------------------------

# @app.post("/extract-metadata")
# def handle_extraction(payload: ExtractMetadataRequest):
#     try:
#         with tempfile.TemporaryDirectory() as tmpdir:
#             local_twbx = os.path.join(tmpdir, "input.twbx")
            
#             # Download
#             download_blob_to_file(payload.inputBlobUrl, local_twbx)
            
#             # Extract
#             metadata = extract_tableau_metadata(local_twbx)
            
#             # Upload
#             base_name = unquote(os.path.basename(payload.inputBlobUrl))
#             output_name = os.path.splitext(base_name)[0] + "_metadata.json"
            
#             output_url = upload_json_to_blob(
#                 payload.outputContainerUrl,
#                 output_name,
#                 metadata
#             )
            
#         return {
#             "status": "success",
#             "outputBlobUrl": output_url,
#             "visuals_found": len(metadata["worksheets"])
#         }

#     except Exception as e:
#         # Log error here in a real app
#         raise HTTPException(status_code=500, detail=str(e))

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)




import json
import os
import zipfile
import tempfile
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote
from typing import List, Dict, Any

# Third-party imports
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.storage.blob import BlobClient

# Initialize App
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

def clean_name(name: str) -> str:
    """Cleans Tableau field names."""
    if not name:
        return ""
    name = name.replace("[", "").replace("]", "")
    name = re.sub(r'^(none|sum|avg|min|max|count|attr|yr|mn|dy|qd|tdc):', '', name, flags=re.IGNORECASE)
    name = re.sub(r':(nk|ok|qk|sk)$', '', name, flags=re.IGNORECASE)
    return name

def get_blob_client(blob_url: str):
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if conn_str:
        try:
            return BlobClient.from_blob_url(blob_url) 
        except Exception:
            pass
    return BlobClient.from_blob_url(blob_url)

def download_blob_to_file(blob_url: str, local_path: str):
    blob = BlobClient.from_blob_url(blob_url)
    with open(local_path, "wb") as f:
        data = blob.download_blob()
        data.readinto(f)

def upload_json_to_blob(container_url: str, blob_name: str, data: dict) -> str:
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        # For testing locally without Azure env, you might want to skip or mock this
        if "test" in container_url: return "http://mock-url"
        raise ValueError("AZURE_STORAGE_CONNECTION_STRING environment variable not set")

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
        "calculatedFields": [],
        "worksheets": [],
        "dashboards": [],
        "globalFilters": [],
        "semanticModel": {
            "tables": [],
            "relationships": [],
            "aggregationHints": {},
            "factTables": []
        }
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        # A. Unzip TWBX
        try:
            with zipfile.ZipFile(twbx_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)
        except zipfile.BadZipFile:
            raise ValueError("File is not a valid .twbx zip file")

        # B. Find .twb XML
        twb_file = None
        for root_dir, _, files in os.walk(tmpdir):
            for file in files:
                if file.endswith(".twb"):
                    twb_file = os.path.join(root_dir, file)
                    break
        
        if not twb_file:
            raise ValueError("No .twb XML file found inside TWBX")

        # C. Parse XML & STRIP NAMESPACES
        try:
            tree = ET.parse(twb_file)
            root = tree.getroot()
        except ET.ParseError:
            raise ValueError("Failed to parse .twb XML content")
        
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        # --- 1. DATASOURCE & SEMANTIC MODEL TABLES ---
        
        # We define a temporary list to hold table schemas for relationship inference later
        extracted_tables = [] # List of dicts {name: str, columns: [names]}

        for datasource in root.findall(".//datasource"):
            ds_name = datasource.get("name")
            
            # Skip 'Parameters' datasource commonly found in Tableau
            if ds_name == 'Parameters':
                continue

            # Populate basic dataSource info (Legacy requirement)
            if not metadata["dataSource"]:
                metadata["dataSource"] = {
                    "name": ds_name,
                    "type": "extract",
                    "tables": []
                }

            # Locate connections and relations
            connection = datasource.find(".//connection")
            if connection:
                # Find all relations that act as tables
                for relation in connection.findall(".//relation"):
                    rel_type = relation.get("type")
                    
                    # Logic: In Tableau XML, type='table' is a physical table.
                    if rel_type == "table":
                        # use 'name' attribute for clean filename (e.g. customers.csv)
                        # use 'table' attribute for internal ID (e.g. [customers#csv])
                        table_name = relation.get("name") 
                        if not table_name:
                            table_name = clean_name(relation.get("table", "UnknownTable"))

                        # Gather columns
                        cols = []
                        col_names_only = []
                        is_fact_table = False

                        for col in relation.findall("./columns/column"):
                            c_name = col.get("name")
                            c_type = col.get("datatype")
                            c_ordinal = col.get("ordinal")
                            
                            cols.append({
                                "name": c_name,
                                "tableDataType": c_type,
                                "ordinal": c_ordinal
                            })
                            col_names_only.append(c_name)

                            # Heuristic: If it contains 'amount', 'price', 'quantity' or is real/integer, likely a fact
                            if c_type in ['real', 'integer'] and any(x in c_name.lower() for x in ['amount', 'price', 'sales', 'profit', 'quantity']):
                                is_fact_table = True

                        # Add to Semantic Model -> Tables
                        table_entry = {
                            "tableName": table_name,
                            "columns": cols
                        }
                        metadata["semanticModel"]["tables"].append(table_entry)
                        
                        # Add to internal list for relationship building
                        extracted_tables.append({
                            "tableName": table_name,
                            "columns": col_names_only
                        })

                        # Add to Semantic Model -> FactTables (Heuristic)
                        if is_fact_table:
                            if table_name not in metadata["semanticModel"]["factTables"]:
                                metadata["semanticModel"]["factTables"].append(table_name)

                        # Add to Legacy dataSource -> tables (simple format)
                        metadata["dataSource"]["tables"].append({
                            "tableName": relation.get("table", table_name), # preserving original logic requested
                            "columns": [] 
                        })

        # --- 2. RELATIONSHIP INFERENCE ---
        # Since the provided XML has separate datasources (Federated), explicit joins might not be present 
        # in a single <relation type='join'> tag. We infer based on common ID columns.
        
        processed_pairs = set()

        for i in range(len(extracted_tables)):
            table_a = extracted_tables[i]
            for j in range(i + 1, len(extracted_tables)):
                table_b = extracted_tables[j]
                
                # Check for common column names ending in 'id' or 'key' or just exact matches
                common_cols = set(table_a["columns"]).intersection(set(table_b["columns"]))
                
                for col in common_cols:
                    # Filter for likely join keys (IDs)
                    if "id" in col.lower() or "key" in col.lower() or "code" in col.lower():
                        
                        # Prevent duplicate relationships
                        pair_id = tuple(sorted((table_a["tableName"], table_b["tableName"])))
                        if pair_id in processed_pairs:
                            continue
                        processed_pairs.add(pair_id)

                        metadata["semanticModel"]["relationships"].append({
                            "fromTable": table_a["tableName"],
                            "fromColumn": col,
                            "toTable": table_b["tableName"],
                            "toColumn": col,
                            "cardinality": "ManyToOne", # Default assumption for star schema
                            "crossFilteringBehavior": "OneDirection"
                        })
                        # Usually only need one join key per table pair
                        break 

        # --- 3. CALCULATED FIELDS ---
        for col in root.findall(".//column"):
            calc = col.find("calculation")
            if calc is not None:
                metadata["calculatedFields"].append({
                    "name": clean_name(col.get("name")),
                    "expression": calc.get("formula")
                })

        # --- 4. WORKSHEETS ---
        for worksheet in root.findall(".//worksheet"):
            sheet_name = worksheet.get('name')
            bound_columns_set = set()

            # Dependency Detection
            for dep in worksheet.findall(".//datasource-dependencies"):
                for col in dep.findall("column-instance"):
                    col_ref = col.get('column')
                    clean_col = None
                    
                    if col_ref:
                        # [some_table].[column_name] -> column_name
                        parts = col_ref.split(']:')
                        if len(parts) > 1:
                            clean_col = clean_name(parts[-1])
                    
                    if not clean_col: 
                        clean_col = clean_name(col.get('name'))
                        
                    if clean_col:
                        bound_columns_set.add(clean_col)

            # Smart Visual Detection
            visual_type = "Automatic"
            
            # A. Check Marks
            for mark_element in worksheet.findall(".//pane/mark"):
                cls = mark_element.get('class')
                if cls and cls != "Automatic":
                    visual_type = MARK_MAP.get(cls.lower(), cls.capitalize())
                    break
            
            # B. Check Style Rules / Heuristics
            if visual_type == "Automatic":
                if worksheet.find(".//style-rule[@element='map']") is not None:
                    visual_type = "Map"
                elif worksheet.find(".//style-rule[@element='table']") is not None:
                    visual_type = "Text Table"
                else:
                    col_list_lower = [c.lower() for c in bound_columns_set]
                    map_keywords = ['lat', 'lon', 'country', 'city', 'state', 'zip', 'geo']
                    
                    if any(k in col for col in col_list_lower for k in map_keywords):
                        visual_type = "Map"
                    elif len(bound_columns_set) == 1:
                        visual_type = "Text Table"
                    else:
                        visual_type = "Bar Chart"

            formatted_columns = [
                {"table": "MainTable", "column": col} 
                for col in sorted(list(bound_columns_set))
            ]

            metadata["worksheets"].append({
                "name": sheet_name,
                "visualType": visual_type, 
                "columns": formatted_columns
            })

        # --- 5. DASHBOARDS ---
        for dashboard in root.findall(".//dashboard"):
            ws_names = []
            for zone in dashboard.findall(".//zone"):
                z_name = zone.get("name")
                if z_name:
                    ws_names.append(z_name)
            
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
            
            # Download
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
        # In production, use logger.error(str(e))
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
