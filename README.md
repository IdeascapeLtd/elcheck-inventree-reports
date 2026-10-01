# Elcheck InvenTree reports (Ideascape)

Two Stock Location reports, powered by one plugin ("Ideascape Stock Reports"):

| Report | Template file | What it shows |
|---|---|---|
| Project Stock Cost Report | report-templates/project_stock_cost_report.html | Every stock item in a project location, its cost, and a total |
| Stock Movement Summary | report-templates/stock_movement_report.html | Per store, per month: opening, in, out, closing (quantity and cost) |

## Step 1 - Install the plugin (choose ONE option, never both)

**Option A - through the InvenTree screen (no server access needed)**
1. Create a GitHub repository named `elcheck-inventree-reports` and upload the
   contents of this folder to it (it can be public - there is no client data or
   password in it).
2. InvenTree: Settings -> Plugins -> Install Plugin
   - Package Name: `ideascape-stock-reports`
   - Source URL: `git+https://github.com/<your-account>/elcheck-inventree-reports.git`
   - Version: leave blank
   - Tick "Confirm plugin installation" -> Install

**Option B - copy a file onto the server**
1. Copy `server-file-option/ideascape_stock_reports.py` into InvenTree's plugins
   folder (Docker: the `plugins` folder inside the InvenTree data folder).
2. Restart InvenTree.

## Step 2 - Enable it
Settings -> Plugins -> find "Ideascape Stock Reports" -> activate it.

## Step 3 - Upload / update the two report templates
Settings -> Reporting (Report Templates) -> for each file in `report-templates`:
- Model type: Stock Location
- Upload the file (replace the old version if it is already there)
- Stock Movement Summary: tick "Landscape"
- Enabled: yes

## Step 4 - Run a report
Open a stock location -> Print -> Report -> choose the template.
If the PDF does not open, allow pop-ups for the InvenTree site, or find it under
Admin Center -> Data Export.
