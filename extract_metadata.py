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
    """
    Cleans Tableau field names.
    """
    if not name:
        return ""
    
    # 1. Remove brackets
    name = name.replace("[", "").replace("]", "")
    
    # 2. Remove Tableau internal patterns (start prefixes)
    name = re.sub(r'^(none|sum|avg|min|max|count|attr|yr|mn|dy|qd|tdc):', '', name, flags=re.IGNORECASE)
    
    # 3. Remove internal suffixes
    name = re.sub(r':(nk|ok|qk|sk)$', '', name, flags=re.IGNORECASE)
    
    return name

def get_blob_client(blob_url: str):
    """
    Helper to get a BlobClient. 
    """
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
        # For local testing without Azure, you might want to bypass this or ensure env var is set
        # raise ValueError("AZURE_STORAGE_CONNECTION_STRING environment variable not set")
        pass

    try:
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
    except Exception as e:
        print(f"Upload failed (Mocking URL for local test): {str(e)}")
        return f"{container_url}/{blob_name}"

# -------------------------------------------------
# CORE EXTRACTION LOGIC
# -------------------------------------------------

def extract_tableau_metadata(twbx_path: str) -> dict:
    metadata = {
        "dataSources": [],  # CHANGED to List to handle multiple datasources
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
        
        # Namespace stripping
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        # ---------------------------------------------
        # 1. DATASOURCES (Global Lookup Map)
        # ---------------------------------------------
        # We need a map to look up datasource info by its technical name (federated.xxxx)
        # to correctly map columns in worksheets later.
        global_ds_lookup = {} 

        # Iterate over ALL datasources, not just the first one
        for datasource in root.findall("datasources/datasource"):
            
            ds_name = datasource.get("name") # e.g., federated.0gc...
            ds_caption = datasource.get("caption", ds_name) # e.g., orders

            ds_info = {
                "name": ds_name,
                "caption": ds_caption,
                "type": "extract",
                "tables": [],
                "connectionInfo": {},
                "rawColumns": {} # Helper map for O(1) lookups
            }

            # Extract Connection Details (Server, DB, Type)
            connection = datasource.find(".//connection")
            if connection is not None:
                ds_info["connectionInfo"] = {
                    "class": connection.get("class"),
                    "dbname": connection.get("dbname"),
                    "server": connection.get("server"),
                    "username": connection.get("username")
                }
                ds_info["type"] = connection.get("class", "extract")

            # Extract Tables/Relations (This is where Relationships live)
            # We look for all 'relation' tags. In Tableau XML, 'relation' defines 
            # the physical or logical tables.
            for relation in datasource.findall(".//relation"):
                table_name = relation.get("table")
                # Fallback: if 'table' attribute is missing, check if it's a logical name
                if not table_name:
                    table_name = relation.get("name") 

                if table_name: 
                    clean_tbl = clean_name(table_name)
                    # Filter out purely internal wrapper objects if necessary, 
                    # but keeping them usually ensures we capture the 'relationship' nodes
                    ds_info["tables"].append({
                        "tableName": clean_tbl,
                        "rawName": table_name,
                        "type": relation.get("type", "table") # 'table', 'join', 'union'
                    })

            # Extract Column Definitions (Data Dictionary)
            for col in datasource.findall(".//column"):
                col_name = col.get("name")
                if col_name:
                    clean_col = clean_name(col_name)
                    ds_info["rawColumns"][clean_col] = {
                        "datatype": col.get("datatype"),
                        "role": col.get("role"),
                        "type": col.get("type"), # quantitative vs nominal
                        "caption": col.get("caption", clean_col)
                    }
            
            # Add to metadata list
            metadata["dataSources"].append({
                "name": ds_info["name"],
                "caption": ds_info["caption"],
                "type": ds_info["type"],
                "tables": ds_info["tables"],
                "connection": ds_info["connectionInfo"]
            })

            # Add to lookup dict for Worksheet processing
            global_ds_lookup[ds_name] = ds_info

        # ---------------------------------------------
        # 2. CALCULATED FIELDS
        # ---------------------------------------------
        for col in root.findall(".//column"):
            calc = col.find("calculation")
            if calc is not None:
                c_name = clean_name(col.get("name"))
                c_type = col.get("datatype", "unknown")
                metadata["calculatedFields"].append({
                    "name": c_name,
                    "expression": calc.get("formula"),
                    "dataType": c_type
                })

        # ---------------------------------------------
        # 3. WORKSHEETS (Fixed Multi-DS Logic)
        # ---------------------------------------------
        for worksheet in root.findall(".//worksheet"):
            sheet_name = worksheet.get('name')
            bound_columns_data = [] 

            # Dependency Detection
            # A worksheet can depend on MULTIPLE datasources.
            # We loop through each dependency block.
            for dep in worksheet.findall(".//datasource-dependencies"):
                
                # Get the technical name of the datasource this block refers to
                ds_ref = dep.get("datasource") 
                
                # Find the corresponding datasource info from our Step 1 lookup
                current_ds_info = global_ds_lookup.get(ds_ref)
                
                # If we can't find the DS info (rare), skip or use empty defaults
                if not current_ds_info:
                    current_ds_info = {"rawColumns": {}, "tables": []}

                for col in dep.findall("column-instance"):
                    col_ref = col.get('column')
                    raw_col_name = col.get('name')
                    clean_col = None
                    detected_table = None

                    # Strategy A: Try to parse [Table].[Column] from the reference
                    if col_ref and '].[' in col_ref:
                        match = re.search(r'^\[(.*?)\]\.\[(.*?)\]', col_ref)
                        if match:
                            detected_table = clean_name(match.group(1))
                            clean_col = clean_name(match.group(2))
                    
                    # Strategy B: Fallback parsing
                    if not clean_col:
                        clean_col = clean_name(raw_col_name)

                    if clean_col:
                        # Lookup data type from the SPECIFIC datasource dictionary
                        col_meta = current_ds_info["rawColumns"].get(clean_col, {})
                        
                        # Determine Table Name:
                        final_table_name = "Extract"
                        
                        if detected_table:
                            final_table_name = detected_table
                        elif current_ds_info["tables"]:
                            # If the column doesn't specify a table (common in CSV),
                            # assign it to the first table of THIS specific datasource.
                            # In your case, this ensures 'order_amount' goes to 'orders#csv'
                            final_table_name = current_ds_info["tables"][0]["tableName"]
                        
                        # Avoid duplicates in this specific sheet
                        if not any(x['column'] == clean_col for x in bound_columns_data):
                            bound_columns_data.append({
                                "sourceDatasource": current_ds_info.get("caption", ds_ref), # Added for clarity
                                "table": final_table_name,
                                "column": clean_col,
                                "dataType": col_meta.get("datatype", "string"),
                                "role": col_meta.get("role", "dimension")
                            })

            # Smart Visual Detection (Preserved exactly as requested)
            visual_type = "Automatic"
            
            # A. Check Marks
            for mark_element in worksheet.findall(".//pane/mark"):
                cls = mark_element.get('class')
                if cls and cls != "Automatic":
                    visual_type = MARK_MAP.get(cls.lower(), cls.capitalize())
                    break
            
            # B. Check Style Rules
            if visual_type == "Automatic":
                if worksheet.find(".//style-rule[@element='map']") is not None:
                    visual_type = "Map"
                elif worksheet.find(".//style-rule[@element='table']") is not None:
                    visual_type = "Text Table"
            
            # C. Guess based on columns
            if visual_type == "Automatic":
                col_list_lower = [c['column'].lower() for c in bound_columns_data]
                map_keywords = ['lat', 'lon', 'country', 'city', 'state', 'zip', 'geo']
                
                if any(k in col for col in col_list_lower for k in map_keywords):
                    visual_type = "Map"
                elif len(bound_columns_data) == 1:
                    visual_type = "Text Table"
                else:
                    visual_type = "Bar Chart"

            metadata["worksheets"].append({
                "name": sheet_name,
                "visualType": visual_type, 
                "columns": bound_columns_data
            })

        # ---------------------------------------------
        # 4. DASHBOARDS
        # ---------------------------------------------
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
            "visuals_found": len(metadata["worksheets"]),
            "datasources_found": len(metadata["dataSources"])
        }

    except Exception as e:
        # Log error here in a real app
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
