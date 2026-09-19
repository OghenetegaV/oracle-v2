"""Oracle — Test Floor Fixture Generator

Purpose:
    Draws a synthetic 10 m x 10 m single-floor DXF (walls, gridlines, circle columns).

Role in Oracle:
    Development utility that (re)creates input_dwgs/test_floor.dxf. Running it as a script
    (python generate_test_dwg.py) OVERWRITES that fixture. Importing it has no side effects.

Dependencies:
    ezdxf; config (INPUT_DIR).

Consumers:
    None (manual run only).

Status:
    Development utility.

Migration:
    Retained; should move to a tools/ folder when fixtures are formalised.
"""

import ezdxf
from config import INPUT_DIR


def main():
    # Create a new DXF (AutoCAD R2018 format)
    doc = ezdxf.new('R2018')

    # Create layers
    doc.layers.add(name='Walls', color=1)
    doc.layers.add(name='Gridlines', color=3)
    doc.layers.add(name='Columns', color=5)

    msp = doc.modelspace()

    # Draw a simple 2-bay × 2-bay office plan (10m × 10m)
    # Outer walls
    msp.add_line((0, 0), (10, 0), dxfattribs={'layer': 'Walls'})
    msp.add_line((10, 0), (10, 10), dxfattribs={'layer': 'Walls'})
    msp.add_line((10, 10), (0, 10), dxfattribs={'layer': 'Walls'})
    msp.add_line((0, 10), (0, 0), dxfattribs={'layer': 'Walls'})

    # Interior wall
    msp.add_line((5, 0), (5, 10), dxfattribs={'layer': 'Walls'})

    # Gridlines
    msp.add_line((0, 5), (10, 5), dxfattribs={'layer': 'Gridlines'})
    msp.add_line((5, 0), (5, 10), dxfattribs={'layer': 'Gridlines'})

    # Columns (circles at grid intersections)
    col_positions = [(0, 0), (0, 5), (0, 10), (5, 0), (5, 5), (5, 10), (10, 0), (10, 5), (10, 10)]
    for x, y in col_positions:
        msp.add_circle((x, y), radius=0.3, dxfattribs={'layer': 'Columns'})

    # Save to input_dwgs folder as DXF
    output_path = INPUT_DIR / "test_floor.dxf"
    doc.saveas(str(output_path))
    print(f"✓ Test DXF created at: {output_path}")


if __name__ == "__main__":
    main()
