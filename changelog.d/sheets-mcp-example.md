bump: minor
Google Sheets: `GoogleSheetsService.from_credentials(credentials)` builds an initialized service from google-auth credentials held in memory, with no client-secrets, credentials or config file and no browser flow. A new example, `examples/sheets_mcp_usage.py`, serves Sheets read and append as MCP tools through `zeo_core.adapters.mcp`.
