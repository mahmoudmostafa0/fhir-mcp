from fhir_mcp_server import mcp

if __name__ == "__main__":
    mcp.run(
    transport="stdio",     # streamable-http
    # host="0.0.0.0",
    # port=8080,            # choose any free port
    # mount_path="/mcp/",         # optional – default is /mcp/
    )