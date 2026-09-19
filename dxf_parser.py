"""Oracle — Architectural DXF Parser (single floor)

Purpose:
    Reads an architectural DXF using fixed layer names (Walls: LINE/LWPOLYLINE, Gridlines: LINE,
    Columns: CIRCLE) and returns plain dicts, optionally saved as output_json/<name>_parsed.json.

Role in Oracle:
    Legacy CAD-interpretation layer for the single-floor path, where Claude then proposes the
    structural layout. It is a different job from ga_dxf_parser.py, which reads an already
    designed multi-floor structural GA; both are currently in use.

Dependencies:
    ezdxf; config (INPUT_DIR, OUTPUT_JSON_DIR).

Consumers:
    oracle_wizard (architectural drawings), oracle_pipeline.

Status:
    Legacy / Transitional.

Migration:
    Retained until a DXF adapter produces oracle.core.BuildingModel and has regression tests.
    Then it can be wrapped by, or folded into, that adapter.
"""

import json
from pathlib import Path
import ezdxf
from config import INPUT_DIR, OUTPUT_JSON_DIR

class DXFParser:
    """Extract structural geometry from DXF"""
    
    def __init__(self, dxf_file):
        self.dxf_file = dxf_file
        self.dxf_path = INPUT_DIR / dxf_file
        self.doc = None
        self.walls = []
        self.columns = []
        self.gridlines = []
        
    def load(self):
        """Load DXF file"""
        try:
            self.doc = ezdxf.readfile(str(self.dxf_path))
            print(f"✓ Loaded: {self.dxf_file}")
            return True
        except Exception as e:
            print(f"❌ Error loading DXF: {e}")
            return False
    
    def extract_walls(self):
        """Extract walls from 'Walls' layer"""
        if not self.doc:
            return []
        
        walls = []
        msp = self.doc.modelspace()
        
        # Query lines on Walls layer
        for entity in msp.query('LINE[layer=="Walls"]'):
            start = (entity.dxf.start.x, entity.dxf.start.y)
            end = (entity.dxf.end.x, entity.dxf.end.y)
            walls.append({
                'type': 'line',
                'start': start,
                'end': end
            })
        
        # Query polylines on Walls layer
        for entity in msp.query('LWPOLYLINE[layer=="Walls"]'):
            points = [tuple(p[:2]) for p in entity.get_points('xy')]
            walls.append({
                'type': 'polyline',
                'points': points
            })
        
        self.walls = walls
        print(f"✓ Extracted {len(walls)} wall segments")
        return walls
    
    def extract_columns(self):
        """Extract columns from 'Columns' layer"""
        if not self.doc:
            return []
        
        columns = []
        msp = self.doc.modelspace()
        
        # Query circles on Columns layer
        for entity in msp.query('CIRCLE[layer=="Columns"]'):
            center = (entity.dxf.center.x, entity.dxf.center.y)
            radius = entity.dxf.radius
            columns.append({
                'type': 'circle',
                'center': center,
                'radius': radius
            })
        
        self.columns = columns
        print(f"✓ Extracted {len(columns)} columns")
        return columns
    
    def extract_gridlines(self):
        """Extract gridlines from 'Gridlines' layer"""
        if not self.doc:
            return []
        
        gridlines = []
        msp = self.doc.modelspace()
        
        # Query lines on Gridlines layer
        for entity in msp.query('LINE[layer=="Gridlines"]'):
            start = (entity.dxf.start.x, entity.dxf.start.y)
            end = (entity.dxf.end.x, entity.dxf.end.y)
            gridlines.append({
                'type': 'line',
                'start': start,
                'end': end
            })
        
        self.gridlines = gridlines
        print(f"✓ Extracted {len(gridlines)} gridlines")
        return gridlines
    
    def parse_all(self):
        """Parse all structural elements"""
        if not self.load():
            return None
        
        self.extract_walls()
        self.extract_columns()
        self.extract_gridlines()
        
        return self.to_dict()
    
    def to_dict(self):
        """Export as dictionary"""
        return {
            'file': str(self.dxf_file),
            'walls': self.walls,
            'columns': self.columns,
            'gridlines': self.gridlines
        }
    
    def to_json(self, output_file=None):
        """Export as JSON file"""
        if output_file is None:
            output_file = self.dxf_path.stem + '_parsed.json'
        
        output_path = OUTPUT_JSON_DIR / output_file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
        
        print(f"✓ JSON saved to: {output_path}")
        return output_path

if __name__ == "__main__":
    parser = DXFParser("test_floor.dxf")
    parser.parse_all()
    parser.to_json()