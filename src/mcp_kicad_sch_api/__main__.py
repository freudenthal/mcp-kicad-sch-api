"""Entry point for running the MCP KiCAD Schematic API server."""

import sys
import asyncio
import traceback
from .server import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server stopped by user", file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        # Print the full traceback, not just the message: this is often the only
        # record of why the server process exited.
        print(f"Server error: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)