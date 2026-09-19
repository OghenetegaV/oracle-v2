"""Oracle — DXF Load Smoke Script

Purpose:
    Opens input_dwgs/test_floor.dxf with ezdxf and prints its layers.

Role in Oracle:
    Manual smoke script, not an automated test: it has no assertions. The automated suite lives
    in tests/.

Dependencies:
    ezdxf; config.

Consumers:
    None (manual run only).

Status:
    Test / manual smoke script (legacy).

Migration:
    Retained; superseded by real fixture-based tests once the DXF adapter exists.
"""

from pathlib import Path
import ezdxf
from config import INPUT_DIR, OUTPUT_DXF_DIR

def load_and_verify_dxf(dxf_file):
    """Load DXF file and verify it can be parsed"""
    
    dxf_path = INPUT_DIR / dxf_file
    
    if not dxf_path.exists():
        print(f"❌ File not found: {dxf_path}")
        return None
    
    try:
        print(f"Loading DXF: {dxf_file}")
        
        # Load DXF directly (no conversion needed)
        doc = ezdxf.readfile(str(dxf_path))
        
        print(f"✓ DXF loaded successfully")
        print(f"  DXF version: {doc.dxfversion}")
        print(f"  Layers: {len(doc.layers)}")
        
        # List layers
        for layer in doc.layers:
            print(f"    - {layer.dxf.name}")
        
        return doc
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

if __name__ == "__main__":
    doc = load_and_verify_dxf("test_floor.dxf")
    if doc:
        print("✓ Ready for parsing")