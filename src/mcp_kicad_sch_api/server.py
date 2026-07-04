"""MCP KiCAD Schematic API Server Implementation

Standard MCP server providing KiCAD schematic manipulation tools.
"""

import asyncio
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Import the KiCAD schematic API
try:
    import kicad_sch_api as ksa
except ImportError as e:
    print(f"Error: Could not import kicad-sch-api: {e}", file=sys.stderr)
    print("Please install: pip install kicad-sch-api", file=sys.stderr)
    sys.exit(1)

# Configure logging to stderr (not stdout for MCP)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger(__name__)


def _dist_version(dist_name: str) -> str:
    """Best-effort installed version of a distribution (never raises)."""
    try:
        from importlib.metadata import version

        return version(dist_name)
    except Exception:
        return "unknown"


def _log_startup_banner() -> None:
    """Record what this process is, to stderr, so a later silent death is
    diagnosable from the client log (which otherwise only shows 'transport
    closed unexpectedly' with no Python context)."""
    ksa_version = getattr(ksa, "__version__", None) or _dist_version("kicad-sch-api")
    logger.info(
        "kicad-sch-api MCP server starting | "
        "server=%s kicad_sch_api=%s mcp=%s | "
        "python=%s pid=%s cwd=%s",
        _dist_version("mcp-kicad-sch-api"),
        ksa_version,
        _dist_version("mcp"),
        sys.version.split()[0],
        os.getpid(),
        os.getcwd(),
    )


# Global schematic instance
current_schematic: Optional[Any] = None

# --- Component search over KiCAD symbol libraries -----------------------------
# kicad-sch-api's SQLite search index (discovery.search_index) cannot be built:
# SymbolLibraryCache._load_library is an unimplemented stub, so bulk symbol
# enumeration returns nothing. Instead we scan the .kicad_sym files the cache
# already discovered, mirroring circuit-synth's tools/find_symbol.py. Top-level
# symbols are indented one tab; deeper (sub-unit) symbols are naturally excluded.
_TOPSYM_RE = re.compile(r'^\t\(symbol "([^"]+)"', re.MULTILINE)
_DESC_RE = re.compile(r'\(property "(?:Description|ki_description)" "([^"]*)"')
_KEYW_RE = re.compile(r'\(property "ki_keywords" "([^"]*)"')


def _search_symbol_libraries(
    query: str, library: Optional[str] = None, limit: int = 20
) -> List[str]:
    """Case-insensitive substring search over discovered KiCAD symbol libraries.

    Matches the query against ``Lib:Symbol`` ids plus each symbol's description
    and keywords. Returns a sorted list of ``Lib:Symbol`` ids (capped at limit).
    """
    try:
        from kicad_sch_api.library.cache import get_symbol_cache

        cache = get_symbol_cache()
        lib_index = dict(getattr(cache, "_library_index", {}) or {})
    except Exception as e:  # cache/library unavailable
        logger.warning(f"search_components: symbol cache unavailable: {e}")
        return []

    q = query.lower()
    hits = set()
    for lib_name, lib_path in sorted(lib_index.items()):
        if library and lib_name.lower() != library.lower():
            continue
        try:
            text = Path(lib_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        marks = list(_TOPSYM_RE.finditer(text))
        for i, m in enumerate(marks):
            sym_name = m.group(1)
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            block = text[m.end() : end]  # this symbol's body (incl. properties)
            desc = _DESC_RE.search(block)
            keyw = _KEYW_RE.search(block)
            haystack = (
                f"{lib_name}:{sym_name} "
                f"{desc.group(1) if desc else ''} "
                f"{keyw.group(1) if keyw else ''}"
            ).lower()
            if q in haystack:
                hits.add(f"{lib_name}:{sym_name}")
    return sorted(hits)[:limit]


async def main():
    """Main MCP server entry point."""
    _log_startup_banner()
    logger.info("Starting MCP KiCAD Schematic API Server...")

    server = Server("mcp-kicad-sch-api")
    
    @server.list_tools()
    async def list_tools() -> List[Tool]:
        """List available KiCAD schematic tools."""
        return [
            Tool(
                name="create_schematic",
                description="Create a new KiCAD schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Name for the schematic"}
                    },
                    "additionalProperties": False
                }
            ),
            Tool(
                name="load_schematic", 
                description="Load an existing KiCAD schematic file",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the .kicad_sch file"}
                    },
                    "required": ["file_path"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="save_schematic",
                description="Save the current schematic to a file", 
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Optional path to save to"}
                    },
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_component",
                description="Add a component to the current schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "lib_id": {"type": "string", "description": "Library ID (e.g., Device:R)"},
                        "reference": {"type": "string", "description": "Component reference (e.g., R1)"},
                        "value": {"type": "string", "description": "Component value (e.g., 10k)"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates"},
                        "footprint": {"type": "string", "description": "Component footprint (e.g., Resistor_SMD:R_0603_1608Metric)"},
                        "properties": {"type": "string", "description": "Additional properties as key=value pairs"}
                    },
                    "required": ["lib_id", "reference", "value", "position"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="search_components",
                description="Search for components in KiCAD symbol libraries",
                inputSchema={
                    "type": "object", 
                    "properties": {
                        "query": {"type": "string", "description": "Search term (e.g., resistor, op amp, 555)"},
                        "library": {"type": "string", "description": "Optional library to search in"},
                        "limit": {"type": "integer", "description": "Maximum number of results"}
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_wire",
                description="Add a wire connection between two points",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "start_pos": {"type": "array", "items": {"type": "number"}, "description": "[x, y] start coordinates"},
                        "end_pos": {"type": "array", "items": {"type": "number"}, "description": "[x, y] end coordinates"}
                    },
                    "required": ["start_pos", "end_pos"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_label",
                description="Add a text label to the schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Label text"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates"},
                        "rotation": {"type": "number", "description": "Text rotation in degrees"},
                        "size": {"type": "number", "description": "Font size"}
                    },
                    "required": ["text", "position"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_hierarchical_label",
                description="Add a hierarchical label to the schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Label text"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates"},
                        "shape": {"type": "string", "description": "Label shape (input, output, bidirectional, tristate, passive, unspecified)"},
                        "rotation": {"type": "number", "description": "Text rotation in degrees"},
                        "size": {"type": "number", "description": "Font size"}
                    },
                    "required": ["text", "position"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_junction",
                description="Add a junction (connection point) to the schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates"},
                        "diameter": {"type": "number", "description": "Junction diameter (optional)"}
                    },
                    "required": ["position"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="list_components",
                description="List all components in the current schematic",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False
                }
            ),
            Tool(
                name="get_schematic_info", 
                description="Get information about the current schematic",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False
                }
            ),
            # NEW: Pin-accurate positioning tools
            Tool(
                name="get_component_pin_position",
                description="Get absolute position of a component pin",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string", "description": "Component reference (e.g., R1)"},
                        "pin_number": {"type": "string", "description": "Pin number (e.g., 1, 2)"}
                    },
                    "required": ["reference", "pin_number"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_label_to_pin",
                description="Add label directly to component pin",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string", "description": "Component reference (e.g., R1)"},
                        "pin_number": {"type": "string", "description": "Pin number (e.g., 1, 2)"},
                        "text": {"type": "string", "description": "Label text"},
                        "offset": {"type": "number", "description": "Offset distance from pin (default: 0)"}
                    },
                    "required": ["reference", "pin_number", "text"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="connect_pins_with_labels",
                description="Connect two component pins using same label",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "comp1_ref": {"type": "string", "description": "First component reference"},
                        "pin1": {"type": "string", "description": "First component pin number"},
                        "comp2_ref": {"type": "string", "description": "Second component reference"},
                        "pin2": {"type": "string", "description": "Second component pin number"},
                        "net_name": {"type": "string", "description": "Net name for connection"}
                    },
                    "required": ["comp1_ref", "pin1", "comp2_ref", "pin2", "net_name"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="list_component_pins",
                description="List all pins for a component with positions",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string", "description": "Component reference (e.g., R1)"}
                    },
                    "required": ["reference"],
                    "additionalProperties": False
                }
            ),
            # Component management
            Tool(
                name="remove_component",
                description="Remove component from schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string", "description": "Component reference to remove"}
                    },
                    "required": ["reference"],
                    "additionalProperties": False
                }
            ),
            # Wire management
            Tool(
                name="remove_wire",
                description="Remove wire from schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "wire_uuid": {"type": "string", "description": "Wire UUID to remove"}
                    },
                    "required": ["wire_uuid"],
                    "additionalProperties": False
                }
            ),
            # Label management
            Tool(
                name="remove_label",
                description="Remove label from schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label_uuid": {"type": "string", "description": "Label UUID to remove"}
                    },
                    "required": ["label_uuid"],
                    "additionalProperties": False
                }
            ),
            # Validation and utility tools
            Tool(
                name="validate_schematic",
                description="Validate schematic for errors and issues",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False
                }
            ),
            Tool(
                name="clone_schematic",
                description="Create a copy of the current schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "new_name": {"type": "string", "description": "Name for cloned schematic (optional)"}
                    },
                    "additionalProperties": False
                }
            ),
            Tool(
                name="backup_schematic",
                description="Create backup of current schematic file",
                inputSchema={
                    "type": "object", 
                    "properties": {
                        "suffix": {"type": "string", "description": "Backup file suffix (default: .backup)"}
                    },
                    "additionalProperties": False
                }
            ),
            # Text elements
            Tool(
                name="add_text",
                description="Add text element to schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text content"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates"},
                        "rotation": {"type": "number", "description": "Text rotation in degrees"},
                        "size": {"type": "number", "description": "Font size"}
                    },
                    "required": ["text", "position"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_text_box",
                description="Add text box element to schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text content"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] top-left coordinates"},
                        "size": {"type": "array", "items": {"type": "number"}, "description": "[width, height] dimensions"},
                        "rotation": {"type": "number", "description": "Text rotation in degrees"},
                        "font_size": {"type": "number", "description": "Font size"}
                    },
                    "required": ["text", "position", "size"],
                    "additionalProperties": False
                }
            ),
            # Hierarchical sheet tools
            Tool(
                name="add_sheet",
                description="Add hierarchical sheet to schematic",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Sheet name"},
                        "filename": {"type": "string", "description": "Sheet filename (.kicad_sch)"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates"},
                        "size": {"type": "array", "items": {"type": "number"}, "description": "[width, height] dimensions"}
                    },
                    "required": ["name", "filename", "position", "size"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="add_sheet_pin",
                description="Add pin to hierarchical sheet",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "sheet_uuid": {"type": "string", "description": "UUID of sheet to add pin to"},
                        "name": {"type": "string", "description": "Pin name"},
                        "pin_type": {"type": "string", "description": "Pin type (input, output, bidirectional)"},
                        "position": {"type": "array", "items": {"type": "number"}, "description": "[x, y] coordinates relative to sheet"}
                    },
                    "required": ["sheet_uuid", "name", "pin_type", "position"],
                    "additionalProperties": False
                }
            ),
            # Component filtering and bulk operations
            Tool(
                name="filter_components",
                description="Filter components by criteria",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "lib_id": {"type": "string", "description": "Filter by library ID (e.g., Device:R)"},
                        "value": {"type": "string", "description": "Filter by component value"},
                        "reference": {"type": "string", "description": "Filter by reference pattern"},
                        "footprint": {"type": "string", "description": "Filter by footprint"}
                    },
                    "additionalProperties": False
                }
            ),
            Tool(
                name="components_in_area",
                description="Find components in rectangular area",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x1": {"type": "number", "description": "Left X coordinate"},
                        "y1": {"type": "number", "description": "Top Y coordinate"},
                        "x2": {"type": "number", "description": "Right X coordinate"},
                        "y2": {"type": "number", "description": "Bottom Y coordinate"}
                    },
                    "required": ["x1", "y1", "x2", "y2"],
                    "additionalProperties": False
                }
            ),
            Tool(
                name="bulk_update_components",
                description="Update multiple components at once",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "criteria": {"type": "object", "description": "Filter criteria (lib_id, value, etc.)"},
                        "updates": {"type": "object", "description": "Updates to apply (value, footprint, properties)"}
                    },
                    "required": ["criteria", "updates"],
                    "additionalProperties": False
                }
            )
        ]
    
    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> List[TextContent]:
        """Handle tool calls by dispatching to appropriate functions."""
        global current_schematic
        
        try:
            if name == "create_schematic":
                schematic_name = arguments.get("name", "untitled")
                logger.info(f"Creating new schematic: {schematic_name}")
                current_schematic = ksa.create_schematic(schematic_name)
                
                return [TextContent(
                    type="text",
                    text=f"✅ Created new KiCAD schematic: '{schematic_name}'"
                )]
                
            elif name == "load_schematic":
                file_path = arguments.get("file_path")
                if not file_path:
                    return [TextContent(
                        type="text",
                        text="❌ file_path parameter is required"
                    )]
                
                logger.info(f"Loading schematic: {file_path}")
                current_schematic = ksa.load_schematic(file_path)
                
                return [TextContent(
                    type="text",
                    text=f"✅ Loaded KiCAD schematic: '{file_path}'"
                )]
                
            elif name == "save_schematic":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                file_path = arguments.get("file_path")
                if file_path:
                    current_schematic.save(file_path)
                    logger.info(f"Saved schematic to: {file_path}")
                    return [TextContent(
                        type="text",
                        text=f"✅ Saved schematic to: {file_path}"
                    )]
                else:
                    current_schematic.save()
                    logger.info("Saved schematic to current file")
                    return [TextContent(
                        type="text", 
                        text="✅ Saved schematic to current file"
                    )]
                    
            elif name == "add_component":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                lib_id = arguments.get("lib_id")
                reference = arguments.get("reference")
                value = arguments.get("value")
                position = arguments.get("position")
                footprint = arguments.get("footprint", "")
                properties = arguments.get("properties", "")
                
                if not all([lib_id, reference, value, position]):
                    return [TextContent(
                        type="text",
                        text="❌ lib_id, reference, value, and position parameters are required"
                    )]
                
                if len(position) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position must be [x, y] coordinates"
                    )]
                
                logger.info(f"Adding component: {lib_id} {reference}={value} at {position}")
                
                # Use the correct API - components.add() method
                component = current_schematic.components.add(
                    lib_id=lib_id,
                    reference=reference,
                    value=value,
                    position=tuple(position),
                    footprint=footprint if footprint else None
                )
                
                # Add additional properties if provided
                if properties:
                    for prop in properties.split(","):
                        if "=" in prop:
                            key, val = prop.strip().split("=", 1)
                            component.set_property(key.strip(), val.strip())
                
                return [TextContent(
                    type="text",
                    text=f"✅ Added component: {reference} ({lib_id}) = {value} at {position}"
                )]
                
            elif name == "search_components":
                query = arguments.get("query")
                if not query:
                    return [TextContent(
                        type="text",
                        text="❌ query parameter is required"
                    )]
                    
                library = arguments.get("library")
                limit = arguments.get("limit") or 20

                logger.info(f"Searching components: {query}")

                results = _search_symbol_libraries(query, library=library, limit=limit)

                if not results:
                    scope = f" in library '{library}'" if library else ""
                    return [TextContent(
                        type="text",
                        text=f"No components found matching '{query}'{scope}."
                    )]

                header = f"Found {len(results)} component(s) matching '{query}':\n\n"
                body = "\n".join(f"• {lib_id}" for lib_id in results)
                return [TextContent(type="text", text=header + body)]
                
            elif name == "add_wire":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                start_pos = arguments.get("start_pos")
                end_pos = arguments.get("end_pos")
                
                if not start_pos or not end_pos:
                    return [TextContent(
                        type="text",
                        text="❌ start_pos and end_pos parameters are required"
                    )]
                
                if len(start_pos) != 2 or len(end_pos) != 2:
                    return [TextContent(
                        type="text", 
                        text="❌ Positions must be [x, y] coordinates"
                    )]
                
                logger.info(f"Adding wire from {start_pos} to {end_pos}")
                
                # Use the correct API method - add_wire with start and end parameters
                wire_uuid = current_schematic.add_wire(
                    start=tuple(start_pos),
                    end=tuple(end_pos)
                )
                
                return [TextContent(
                    type="text",
                    text=f"✅ Added wire from {start_pos} to {end_pos} (UUID: {wire_uuid})"
                )]
                
            elif name == "add_label":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                text = arguments.get("text")
                position = arguments.get("position")
                rotation = arguments.get("rotation", 0.0)
                size = arguments.get("size", 1.27)
                
                if not text or not position:
                    return [TextContent(
                        type="text",
                        text="❌ text and position parameters are required"
                    )]
                
                if len(position) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position must be [x, y] coordinates"
                    )]
                
                logger.info(f"Adding label '{text}' at {position}")
                
                # Use the correct API method
                label_uuid = current_schematic.add_label(
                    text=text,
                    position=tuple(position),
                    rotation=rotation,
                    size=size
                )
                
                return [TextContent(
                    type="text",
                    text=f"✅ Added label '{text}' at {position} (UUID: {label_uuid})"
                )]
                
            elif name == "add_hierarchical_label":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                text = arguments.get("text")
                position = arguments.get("position")
                shape = arguments.get("shape", "input")
                rotation = arguments.get("rotation", 0.0)
                size = arguments.get("size", 1.27)
                
                if not text or not position:
                    return [TextContent(
                        type="text",
                        text="❌ text and position parameters are required"
                    )]
                
                if len(position) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position must be [x, y] coordinates"
                    )]
                
                logger.info(f"Adding hierarchical label '{text}' at {position}")
                
                # Import the shape enum
                from kicad_sch_api.core.types import HierarchicalLabelShape
                
                # Convert string shape to enum
                shape_map = {
                    "input": HierarchicalLabelShape.INPUT,
                    "output": HierarchicalLabelShape.OUTPUT,
                    "bidirectional": HierarchicalLabelShape.BIDIRECTIONAL,
                    "tristate": HierarchicalLabelShape.TRISTATE,
                    "passive": HierarchicalLabelShape.PASSIVE,
                    "unspecified": HierarchicalLabelShape.UNSPECIFIED
                }
                
                shape_enum = shape_map.get(shape.lower(), HierarchicalLabelShape.INPUT)
                
                # Use the correct API method
                label_uuid = current_schematic.add_hierarchical_label(
                    text=text,
                    position=tuple(position),
                    shape=shape_enum,
                    rotation=rotation,
                    size=size
                )
                
                return [TextContent(
                    type="text",
                    text=f"✅ Added hierarchical label '{text}' ({shape}) at {position} (UUID: {label_uuid})"
                )]
                
            elif name == "add_junction":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                position = arguments.get("position")
                diameter = arguments.get("diameter", 0.0)
                
                if not position:
                    return [TextContent(
                        type="text",
                        text="❌ position parameter is required"
                    )]
                
                if len(position) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position must be [x, y] coordinates"
                    )]
                
                logger.info(f"Adding junction at {position}")
                
                # Use the junction collection to add junction
                junction_uuid = current_schematic.junctions.add(
                    position=tuple(position),
                    diameter=diameter
                )
                
                return [TextContent(
                    type="text",
                    text=f"✅ Added junction at {position} (UUID: {junction_uuid})"
                )]
                
            elif name == "list_components":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                components = list(current_schematic.components)
                
                if not components:
                    return [TextContent(
                        type="text",
                        text="No components in the current schematic."
                    )]
                
                result_text = f"Components in schematic ({len(components)} total):\n\n"
                for comp in components:
                    result_text += f"• {comp.reference} ({comp.lib_id}) = {comp.value}"
                    if hasattr(comp, 'position'):
                        result_text += f" at {comp.position}"
                    if hasattr(comp, 'footprint') and comp.footprint:
                        result_text += f" [{comp.footprint}]"
                    result_text += "\n"
                
                return [TextContent(type="text", text=result_text)]
                
            elif name == "get_schematic_info":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                # Get comprehensive schematic information
                try:
                    summary = current_schematic.get_summary()
                    
                    info_text = "📋 Schematic Information:\n\n"
                    info_text += f"• Title: {summary.get('title', 'Untitled')}\n"
                    info_text += f"• Components: {summary.get('component_count', 0)}\n"
                    info_text += f"• Modified: {summary.get('modified', False)}\n"
                    
                    # Additional stats if available
                    if hasattr(current_schematic, 'wires'):
                        info_text += f"• Wires: {len(current_schematic.wires)}\n"
                    if hasattr(current_schematic, 'junctions'):
                        info_text += f"• Junctions: {len(current_schematic.junctions)}\n"
                    
                    return [TextContent(type="text", text=info_text)]
                except Exception as e:
                    # Fallback info
                    info_text = "📋 Schematic Information:\n\n"
                    info_text += f"• Components: {len(list(current_schematic.components))}\n"
                    info_text += f"• Status: Loaded and ready\n"
                    
                    return [TextContent(type="text", text=info_text)]
                    
            # NEW: Pin-accurate positioning tools
            elif name == "get_component_pin_position":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                reference = arguments.get("reference")
                pin_number = arguments.get("pin_number")
                
                if not reference or not pin_number:
                    return [TextContent(
                        type="text",
                        text="❌ reference and pin_number parameters are required"
                    )]
                
                try:
                    pin_pos = current_schematic.get_component_pin_position(reference, pin_number)
                    if pin_pos:
                        return [TextContent(
                            type="text",
                            text=f"✅ {reference} pin {pin_number} position: ({pin_pos.x:.3f}, {pin_pos.y:.3f})"
                        )]
                    else:
                        return [TextContent(
                            type="text",
                            text=f"❌ Pin {pin_number} not found on component {reference}"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error getting pin position: {str(e)}"
                    )]
                    
            elif name == "add_label_to_pin":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                reference = arguments.get("reference")
                pin_number = arguments.get("pin_number")
                text = arguments.get("text")
                offset = arguments.get("offset", 0.0)
                
                if not all([reference, pin_number, text]):
                    return [TextContent(
                        type="text",
                        text="❌ reference, pin_number, and text parameters are required"
                    )]
                
                try:
                    label_uuid = current_schematic.add_label_to_pin(reference, pin_number, text, offset)
                    return [TextContent(
                        type="text",
                        text=f"✅ Added label '{text}' to {reference} pin {pin_number} (UUID: {label_uuid})"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error adding label to pin: {str(e)}"
                    )]
                    
            elif name == "connect_pins_with_labels":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                comp1_ref = arguments.get("comp1_ref")
                pin1 = arguments.get("pin1")
                comp2_ref = arguments.get("comp2_ref")
                pin2 = arguments.get("pin2")
                net_name = arguments.get("net_name")
                
                if not all([comp1_ref, pin1, comp2_ref, pin2, net_name]):
                    return [TextContent(
                        type="text",
                        text="❌ All parameters required: comp1_ref, pin1, comp2_ref, pin2, net_name"
                    )]
                
                try:
                    label_uuids = current_schematic.connect_pins_with_labels(comp1_ref, pin1, comp2_ref, pin2, net_name)
                    return [TextContent(
                        type="text",
                        text=f"✅ Connected {comp1_ref}:{pin1} to {comp2_ref}:{pin2} with net '{net_name}' ({len(label_uuids)} labels created)"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error connecting pins: {str(e)}"
                    )]
                    
            elif name == "list_component_pins":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                reference = arguments.get("reference")
                if not reference:
                    return [TextContent(
                        type="text",
                        text="❌ reference parameter is required"
                    )]
                
                try:
                    pins = current_schematic.list_component_pins(reference)
                    if pins:
                        pins_text = f"📍 {reference} pins:\n\n"
                        for pin_num, pin_pos in pins:
                            pins_text += f"• Pin {pin_num}: ({pin_pos.x:.3f}, {pin_pos.y:.3f})\n"
                        return [TextContent(type="text", text=pins_text)]
                    else:
                        return [TextContent(
                            type="text",
                            text=f"❌ No pins found for component {reference}"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error listing pins: {str(e)}"
                    )]
                    
            # Component management
            elif name == "remove_component":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                reference = arguments.get("reference")
                if not reference:
                    return [TextContent(
                        type="text",
                        text="❌ reference parameter is required"
                    )]
                
                try:
                    removed = current_schematic.components.remove(reference)
                    if removed:
                        return [TextContent(
                            type="text",
                            text=f"✅ Removed component {reference}"
                        )]
                    else:
                        return [TextContent(
                            type="text",
                            text=f"❌ Component {reference} not found"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error removing component: {str(e)}"
                    )]
                    
            elif name == "remove_wire":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                wire_uuid = arguments.get("wire_uuid")
                if not wire_uuid:
                    return [TextContent(
                        type="text",
                        text="❌ wire_uuid parameter is required"
                    )]
                
                try:
                    removed = current_schematic.remove_wire(wire_uuid)
                    if removed:
                        return [TextContent(
                            type="text",
                            text=f"✅ Removed wire {wire_uuid}"
                        )]
                    else:
                        return [TextContent(
                            type="text",
                            text=f"❌ Wire {wire_uuid} not found"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error removing wire: {str(e)}"
                    )]
                    
            elif name == "remove_label":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                label_uuid = arguments.get("label_uuid")
                if not label_uuid:
                    return [TextContent(
                        type="text",
                        text="❌ label_uuid parameter is required"
                    )]
                
                try:
                    removed = current_schematic.remove_label(label_uuid)
                    if removed:
                        return [TextContent(
                            type="text",
                            text=f"✅ Removed label {label_uuid}"
                        )]
                    else:
                        return [TextContent(
                            type="text",
                            text=f"❌ Label {label_uuid} not found"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error removing label: {str(e)}"
                    )]
                    
            # Validation and utility tools
            elif name == "validate_schematic":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                try:
                    issues = current_schematic.validate()
                    
                    if not issues:
                        return [TextContent(
                            type="text",
                            text="✅ Schematic validation passed - no issues found"
                        )]
                    
                    errors = [issue for issue in issues if issue.level.value in ("error", "critical")]
                    warnings = [issue for issue in issues if issue.level.value == "warning"]
                    
                    result_text = f"📋 Validation Results:\n\n"
                    result_text += f"• Total issues: {len(issues)}\n"
                    result_text += f"• Errors: {len(errors)}\n"
                    result_text += f"• Warnings: {len(warnings)}\n\n"
                    
                    if errors:
                        result_text += "❌ Critical Errors:\n"
                        for error in errors[:5]:  # Show first 5
                            result_text += f"  - {error}\n"
                    
                    if warnings:
                        result_text += "\n⚠️ Warnings:\n"
                        for warning in warnings[:5]:  # Show first 5
                            result_text += f"  - {warning}\n"
                    
                    return [TextContent(type="text", text=result_text)]
                    
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error validating schematic: {str(e)}"
                    )]
                    
            elif name == "clone_schematic":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                new_name = arguments.get("new_name")
                
                try:
                    cloned = current_schematic.clone(new_name)
                    # Note: Not switching to cloned schematic to avoid state confusion
                    
                    return [TextContent(
                        type="text",
                        text=f"✅ Created schematic clone: '{new_name or 'Clone'}' with {len(list(cloned.components))} components"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error cloning schematic: {str(e)}"
                    )]
                    
            elif name == "backup_schematic":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                suffix = arguments.get("suffix", ".backup")
                
                try:
                    backup_path = current_schematic.backup(suffix)
                    return [TextContent(
                        type="text",
                        text=f"✅ Created backup: {backup_path}"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error creating backup: {str(e)}"
                    )]
                    
            # Text elements
            elif name == "add_text":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                text = arguments.get("text")
                position = arguments.get("position")
                rotation = arguments.get("rotation", 0.0)
                size = arguments.get("size", 1.27)
                
                if not text or not position:
                    return [TextContent(
                        type="text",
                        text="❌ text and position parameters are required"
                    )]
                
                if len(position) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position must be [x, y] coordinates"
                    )]
                
                try:
                    text_uuid = current_schematic.add_text(text, tuple(position), rotation, size)
                    return [TextContent(
                        type="text",
                        text=f"✅ Added text '{text}' at {position} (UUID: {text_uuid})"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error adding text: {str(e)}"
                    )]
                    
            elif name == "add_text_box":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                text = arguments.get("text")
                position = arguments.get("position")
                size = arguments.get("size")
                rotation = arguments.get("rotation", 0.0)
                font_size = arguments.get("font_size", 1.27)
                
                if not all([text, position, size]):
                    return [TextContent(
                        type="text",
                        text="❌ text, position, and size parameters are required"
                    )]
                
                if len(position) != 2 or len(size) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position and size must be [x, y] and [width, height] arrays"
                    )]
                
                try:
                    textbox_uuid = current_schematic.add_text_box(
                        text, tuple(position), tuple(size), rotation, font_size
                    )
                    return [TextContent(
                        type="text",
                        text=f"✅ Added text box '{text}' at {position} size {size} (UUID: {textbox_uuid})"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error adding text box: {str(e)}"
                    )]
                    
            # Hierarchical sheet tools
            elif name == "add_sheet":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                name_arg = arguments.get("name")
                filename = arguments.get("filename")
                position = arguments.get("position")
                size = arguments.get("size")
                
                if not all([name_arg, filename, position, size]):
                    return [TextContent(
                        type="text",
                        text="❌ name, filename, position, and size parameters are required"
                    )]
                
                if len(position) != 2 or len(size) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position and size must be [x, y] and [width, height] arrays"
                    )]
                
                try:
                    sheet_uuid = current_schematic.add_sheet(
                        name_arg, filename, tuple(position), tuple(size)
                    )
                    return [TextContent(
                        type="text",
                        text=f"✅ Added hierarchical sheet '{name_arg}' ({filename}) at {position} (UUID: {sheet_uuid})"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error adding sheet: {str(e)}"
                    )]
                    
            elif name == "add_sheet_pin":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                sheet_uuid = arguments.get("sheet_uuid")
                name_arg = arguments.get("name")
                pin_type = arguments.get("pin_type")
                position = arguments.get("position")
                
                if not all([sheet_uuid, name_arg, pin_type, position]):
                    return [TextContent(
                        type="text",
                        text="❌ sheet_uuid, name, pin_type, and position parameters are required"
                    )]
                
                if len(position) != 2:
                    return [TextContent(
                        type="text",
                        text="❌ Position must be [x, y] coordinates"
                    )]
                
                try:
                    pin_uuid = current_schematic.add_sheet_pin(
                        sheet_uuid, name_arg, pin_type, tuple(position)
                    )
                    return [TextContent(
                        type="text",
                        text=f"✅ Added sheet pin '{name_arg}' ({pin_type}) to sheet {sheet_uuid} (UUID: {pin_uuid})"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error adding sheet pin: {str(e)}"
                    )]
                    
            # Component filtering and bulk operations
            elif name == "filter_components":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                # Build filter criteria from arguments
                criteria = {}
                for key in ["lib_id", "value", "reference", "footprint"]:
                    if arguments.get(key):
                        criteria[key] = arguments[key]
                
                if not criteria:
                    return [TextContent(
                        type="text",
                        text="❌ At least one filter criteria required (lib_id, value, reference, footprint)"
                    )]
                
                try:
                    filtered_components = current_schematic.components.filter(**criteria)
                    
                    if filtered_components:
                        result_text = f"🔍 Found {len(filtered_components)} components matching criteria:\n\n"
                        for comp in filtered_components[:10]:  # Show first 10
                            result_text += f"• {comp.reference} ({comp.lib_id}) = {comp.value}\n"
                        
                        if len(filtered_components) > 10:
                            result_text += f"\n... and {len(filtered_components) - 10} more"
                            
                        return [TextContent(type="text", text=result_text)]
                    else:
                        return [TextContent(
                            type="text",
                            text="❌ No components found matching criteria"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error filtering components: {str(e)}"
                    )]
                    
            elif name == "components_in_area":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                x1 = arguments.get("x1")
                y1 = arguments.get("y1")
                x2 = arguments.get("x2")
                y2 = arguments.get("y2")
                
                if not all([x1 is not None, y1 is not None, x2 is not None, y2 is not None]):
                    return [TextContent(
                        type="text",
                        text="❌ x1, y1, x2, y2 coordinates are required"
                    )]
                
                try:
                    components_in_area = current_schematic.components.in_area(x1, y1, x2, y2)
                    
                    if components_in_area:
                        result_text = f"📍 Found {len(components_in_area)} components in area ({x1}, {y1}) to ({x2}, {y2}):\n\n"
                        for comp in components_in_area:
                            result_text += f"• {comp.reference} at ({comp.position.x:.1f}, {comp.position.y:.1f})\n"
                            
                        return [TextContent(type="text", text=result_text)]
                    else:
                        return [TextContent(
                            type="text",
                            text=f"❌ No components found in area ({x1}, {y1}) to ({x2}, {y2})"
                        )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error finding components in area: {str(e)}"
                    )]
                    
            elif name == "bulk_update_components":
                if current_schematic is None:
                    return [TextContent(
                        type="text",
                        text="❌ No schematic loaded. Create or load a schematic first."
                    )]
                
                criteria = arguments.get("criteria")
                updates = arguments.get("updates")
                
                if not criteria or not updates:
                    return [TextContent(
                        type="text",
                        text="❌ criteria and updates parameters are required"
                    )]
                
                try:
                    updated_count = current_schematic.components.bulk_update(
                        criteria=criteria, updates=updates
                    )
                    return [TextContent(
                        type="text",
                        text=f"✅ Bulk updated {updated_count} components"
                    )]
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"❌ Error bulk updating components: {str(e)}"
                    )]
            
            else:
                return [TextContent(
                    type="text",
                    text=f"❌ Unknown tool: {name}"
                )]
                
        except Exception as e:
            logger.error(f"Error in tool {name}: {e}")
            return [TextContent(
                type="text",
                text=f"❌ Error in {name}: {str(e)}"
            )]
    
    # Start the MCP server
    logger.info("MCP server ready, waiting for connections...")

    options = server.create_initialization_options()
    exit_reason = "clean shutdown (client closed the transport)"
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                options,
                raise_exceptions=True,
            )
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        exit_reason = f"interrupted ({type(e).__name__})"
        raise
    except BaseException as e:
        # Make an otherwise-silent death explain itself: without this, the client
        # log only shows "transport closed unexpectedly" with no Python context.
        exit_reason = f"unhandled {type(e).__name__}: {e}"
        logger.error("MCP server exiting on unhandled exception: %s", e)
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        # Always leave a breadcrumb, whatever the exit path.
        logger.info("MCP server run loop exited: %s", exit_reason)


if __name__ == "__main__":
    asyncio.run(main())