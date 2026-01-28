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


# gemini part--------------------------------------------------------------------------------------------------------

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

# def clean_table_name(table_ref: str) -> str:
#     """
#     Extract clean table name from Tableau reference.
#     Examples:
#     - [federated.xxx].[TableName] -> TableName
#     - [Extract].[Extract] -> Extract
#     - TableName -> TableName
#     """
#     if not table_ref:
#         return "MainTable"
    
#     # Remove outer brackets
#     table_ref = table_ref.strip('[]')
    
#     # Split by ].[ pattern to get last part
#     if '].[' in table_ref:
#         parts = table_ref.split('].[')
#         return clean_name(parts[-1])
    
#     # Split by . to get last part
#     if '.' in table_ref:
#         parts = table_ref.split('.')
#         return clean_name(parts[-1])
    
#     return clean_name(table_ref)

# def get_blob_client(blob_url: str):
#     """
#     Helper to get a BlobClient. 
#     Tries to use Connection String if available to handle Auth,
#     otherwise falls back to the URL (assuming SAS token exists).
#     """
#     conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    
#     if conn_str:
#         try:
#             return BlobClient.from_blob_url(blob_url) 
#         except Exception:
#             pass
            
#     return BlobClient.from_blob_url(blob_url)

# def download_blob_to_file(blob_url: str, local_path: str):
#     blob = BlobClient.from_blob_url(blob_url)
    
#     with open(local_path, "wb") as f:
#         data = blob.download_blob()
#         data.readinto(f)

# def upload_json_to_blob(container_url: str, blob_name: str, data: dict) -> str:
#     conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
#     if not conn_str:
#         raise ValueError("AZURE_STORAGE_CONNECTION_STRING environment variable not set")

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

# def extract_table_columns(root):
#     """
#     Extract all columns for each table from datasource dependencies.
#     Returns: dict mapping table_name -> list of column names
#     """
#     table_columns = {}
    
#     # Look through all datasource-dependencies to find columns
#     for dep in root.findall(".//datasource-dependencies"):
#         datasource_name = dep.get("datasource")
        
#         for col_inst in dep.findall("column-instance"):
#             col_ref = col_inst.get("column")
#             if not col_ref:
#                 continue
            
#             # Parse [table].[column] format
#             if '].[' in col_ref:
#                 parts = col_ref.split('].[')
#                 if len(parts) >= 2:
#                     table_part = parts[0].strip('[')
#                     table_name = clean_table_name(table_part)
#                     col_name = clean_name(parts[-1])
                    
#                     if table_name not in table_columns:
#                         table_columns[table_name] = set()
#                     table_columns[table_name].add(col_name)
    
#     # Convert sets to sorted lists
#     return {table: sorted(list(cols)) for table, cols in table_columns.items()}

# def extract_relationships(root):
#     """
#     Extract table relationships from Tableau metadata.
#     Returns: list of relationship objects
#     """
#     relationships = []
    
#     # Look for relationship definitions in datasource
#     for datasource in root.findall(".//datasource"):
#         # Check for joins
#         for relation in datasource.findall(".//relation[@join]"):
#             join_type = relation.get("join")
            
#             # Try to find left and right tables
#             left_table = relation.find(".//relation[@table]")
#             right_table = relation.findall(".//relation[@table]")
            
#             if len(right_table) > 1:
#                 left_name = clean_table_name(left_table.get("table")) if left_table is not None else None
#                 right_name = clean_table_name(right_table[1].get("table"))
                
#                 # Extract join clause if available
#                 clause = relation.find(".//clause")
#                 from_col = None
#                 to_col = None
                
#                 if clause is not None:
#                     # Parse expression like [table1].[col1] = [table2].[col2]
#                     expr = clause.get("expression", "")
#                     if "=" in expr:
#                         parts = expr.split("=")
#                         if len(parts) == 2:
#                             left_expr = parts[0].strip()
#                             right_expr = parts[1].strip()
                            
#                             # Extract column names
#                             if '].[' in left_expr:
#                                 from_col = clean_name(left_expr.split('].[')[-1])
#                             if '].[' in right_expr:
#                                 to_col = clean_name(right_expr.split('].[')[-1])
                
#                 if left_name and right_name:
#                     relationships.append({
#                         "name": f"Rel_{left_name}_{right_name}",
#                         "fromTable": left_name,
#                         "fromColumn": from_col or "id",
#                         "toTable": right_name,
#                         "toColumn": to_col or "id",
#                         "joinType": join_type
#                     })
    
#     return relationships

# def map_column_to_table(col_ref: str, table_columns: dict) -> str:
#     """
#     Map a column reference to its source table.
#     """
#     if not col_ref:
#         return "MainTable"
    
#     # Try to extract table from reference like [table].[column]
#     if '].[' in col_ref:
#         parts = col_ref.split('].[')
#         if len(parts) >= 2:
#             table_part = parts[0].strip('[')
#             return clean_table_name(table_part)
    
#     # Fallback: search which table contains this column
#     clean_col = clean_name(col_ref)
#     for table_name, columns in table_columns.items():
#         if clean_col in columns:
#             return table_name
    
#     # Default fallback
#     return list(table_columns.keys())[0] if table_columns else "MainTable"

# def extract_tableau_metadata(twbx_path: str) -> dict:
#     metadata = {
#         "dataSource": {},
#         "calculatedFields": [],
#         "worksheets": [],
#         "dashboards": [],
#         "globalFilters": [],
#         "tables": [],
#         "relationships": []
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

#         # Extract table-column mapping
#         table_columns = extract_table_columns(root)

#         # 1. DATASOURCE & TABLES
#         datasource = root.find(".//datasource")
#         if datasource is not None:
#             tables = []
            
#             # Extract from relations
#             for relation in datasource.findall(".//relation"):
#                 table_name = relation.get("table")
#                 if not table_name: 
#                     continue
                
#                 clean_tbl_name = clean_table_name(table_name)
#                 columns = table_columns.get(clean_tbl_name, [])
                
#                 tables.append({
#                     "name": clean_tbl_name,
#                     "columns": columns
#                 })
            
#             # Also check extracted table_columns for any missed tables
#             for tbl_name, cols in table_columns.items():
#                 if not any(t["name"] == tbl_name for t in tables):
#                     tables.append({
#                         "name": tbl_name,
#                         "columns": cols
#                     })

#             metadata["dataSource"] = {
#                 "name": datasource.get("name") or "TableauData",
#                 "type": "extract",
#                 "tables": tables
#             }
            
#             metadata["tables"] = tables

#         # 2. RELATIONSHIPS
#         metadata["relationships"] = extract_relationships(root)

#         # 3. CALCULATED FIELDS
#         for col in root.findall(".//column"):
#             calc = col.find("calculation")
#             if calc is not None:
#                 metadata["calculatedFields"].append({
#                     "name": clean_name(col.get("name")),
#                     "expression": calc.get("formula")
#                 })

#         # 4. WORKSHEETS
#         for worksheet in root.findall(".//worksheet"):
#             sheet_name = worksheet.get('name')
#             bound_columns_map = {}  # table -> [columns]

#             # Dependency Detection
#             for dep in worksheet.findall(".//datasource-dependencies"):
#                 for col in dep.findall("column-instance"):
#                     col_ref = col.get('column')
                    
#                     if col_ref:
#                         table_name = map_column_to_table(col_ref, table_columns)
                        
#                         # Extract column name
#                         if '].[' in col_ref:
#                             parts = col_ref.split(']:')
#                             if len(parts) > 1:
#                                 clean_col = clean_name(parts[-1])
#                             else:
#                                 clean_col = clean_name(col_ref.split('].[')[-1])
#                         else:
#                             clean_col = clean_name(col.get('name'))
                        
#                         if clean_col:
#                             if table_name not in bound_columns_map:
#                                 bound_columns_map[table_name] = set()
#                             bound_columns_map[table_name].add(clean_col)

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
#                 all_cols = [col for cols in bound_columns_map.values() for col in cols]
#                 col_list_lower = [c.lower() for c in all_cols]
#                 map_keywords = ['lat', 'lon', 'country', 'city', 'state', 'zip', 'geo']
                
#                 if any(k in col for col in col_list_lower for k in map_keywords):
#                     visual_type = "Map"
#                 elif len(all_cols) == 1:
#                     visual_type = "Text Table"
#                 else:
#                     visual_type = "Bar Chart"

#             # Format columns with actual table names
#             formatted_columns = []
#             for table_name, cols in bound_columns_map.items():
#                 for col in sorted(list(cols)):
#                     formatted_columns.append({
#                         "table": table_name,
#                         "column": col
#                     })

#             metadata["worksheets"].append({
#                 "name": sheet_name,
#                 "visualType": visual_type, 
#                 "columns": formatted_columns
#             })

#         # 5. DASHBOARDS
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
#             "visuals_found": len(metadata["worksheets"]),
#             "tables_found": len(metadata.get("tables", [])),
#             "relationships_found": len(metadata.get("relationships", []))
#         }

#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)



# chatgpt part---------------------------------------------------------------------

import json
import os
import zipfile
import tempfile
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from azure.storage.blob import BlobClient

app = FastAPI(title="Tableau Metadata Extractor API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    if not name:
        return ""
    name = name.replace("[", "").replace("]", "")
    name = re.sub(r'^(sum|avg|min|max|count|attr):', '', name, flags=re.IGNORECASE)
    name = re.sub(r':\w+$', '', name)
    return name


def download_blob_to_file(blob_url: str, local_path: str):
    blob = BlobClient.from_blob_url(blob_url)
    with open(local_path, "wb") as f:
        blob.download_blob().readinto(f)


def upload_json_to_blob(container_url: str, blob_name: str, data: dict) -> str:
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    container_name = container_url.rstrip("/").split("/")[-1]
    blob = BlobClient.from_connection_string(
        conn_str, container_name, blob_name
    )
    blob.upload_blob(json.dumps(data, indent=2), overwrite=True)
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
        "model": {
            "tables": [],
            "relationships": []
        }
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(twbx_path, "r") as zip_ref:
            zip_ref.extractall(tmpdir)

        twb_file = next(
            os.path.join(r, f)
            for r, _, files in os.walk(tmpdir)
            for f in files if f.endswith(".twb")
        )

        tree = ET.parse(twb_file)
        root = tree.getroot()

        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]

        # -------------------------------
        # TABLES
        # -------------------------------
        table_columns = {}

        for relation in root.findall(".//relation[@type='table']"):
            table_name = clean_name(relation.get("table") or relation.get("name"))
            if not table_name:
                continue

            cols = []
            for col in relation.findall(".//column"):
                cols.append(clean_name(col.get("name")))

            table_columns[table_name] = cols
            metadata["model"]["tables"].append({
                "name": table_name,
                "columns": cols
            })

        # -------------------------------
        # RELATIONSHIPS
        # -------------------------------
        for join in root.findall(".//relation[@type='join']"):
            clauses = join.findall(".//clause")
            for clause in clauses:
                expr = clause.find("expression")
                if expr is None:
                    continue

                left = expr.find("expression")
                right = expr.findall("expression")[1]

                def parse_ref(e):
                    parts = e.text.replace("[", "").replace("]", "").split(".")
                    return parts[0], parts[-1]

                l_table, l_col = parse_ref(left)
                r_table, r_col = parse_ref(right)

                metadata["model"]["relationships"].append({
                    "name": f"Rel_{l_table}_{r_table}",
                    "fromTable": l_table,
                    "fromColumn": l_col,
                    "toTable": r_table,
                    "toColumn": r_col
                })

        # -------------------------------
        # WORKSHEETS (FIXED TABLE NAME)
        # -------------------------------
        for worksheet in root.findall(".//worksheet"):
            bound_cols = []

            for col in worksheet.findall(".//column-instance"):
                col_ref = col.get("column")
                if not col_ref:
                    continue

                parts = col_ref.replace("[", "").replace("]", "").split(".")
                table = parts[0]
                column = parts[-1]

                bound_cols.append({
                    "table": table,
                    "column": clean_name(column)
                })

            visual_type = "Automatic"
            for mark in worksheet.findall(".//mark"):
                cls = mark.get("class")
                if cls and cls.lower() in MARK_MAP:
                    visual_type = MARK_MAP[cls.lower()]
                    break

            metadata["worksheets"].append({
                "name": worksheet.get("name"),
                "visualType": visual_type,
                "columns": bound_cols
            })

    return metadata


# -------------------------------------------------
# API
# -------------------------------------------------

@app.post("/extract-metadata")
def handle_extraction(payload: ExtractMetadataRequest):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            local_file = os.path.join(tmpdir, "input.twbx")
            download_blob_to_file(payload.inputBlobUrl, local_file)
            metadata = extract_tableau_metadata(local_file)

            output_name = unquote(
                os.path.basename(payload.inputBlobUrl)
            ).replace(".twbx", "_metadata.json")

            url = upload_json_to_blob(
                payload.outputContainerUrl,
                output_name,
                metadata
            )

        return {
            "status": "success",
            "outputBlobUrl": url,
            "visuals_found": len(metadata["worksheets"])
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
