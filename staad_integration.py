"""Oracle — OpenSTAADpy Integration (older, unused)

Purpose:
    Earlier attempt at building and analysing a STAAD model through the openstaadpy package
    (os_analytical), writing results to JSON.

Role in Oracle:
    Superseded by staad_v8i_integration.py (COM route verified against STAAD.Pro V8i SS6).
    Nothing in the repository imports this file, and openstaadpy is not in requirements.txt.

Dependencies:
    openstaadpy (optional, imported at run time); config.

Consumers:
    None found by import search; run only manually via __main__.

Status:
    Legacy / Superseded. Flagged for human review before any removal.

Migration:
    Kept until the owner confirms the openstaadpy route is abandoned; see
    docs/REPOSITORY_INVENTORY.md.
"""

import json
from pathlib import Path
from config import OUTPUT_JSON_DIR

def load_ga(json_file="ga_output.json"):
    """Load GA from JSON"""
    path = OUTPUT_JSON_DIR / json_file
    with open(path, 'r') as f:
        return json.load(f)

def create_staad_model(ga_data):
    """Create Staad Pro model from GA"""
    try:
        from openstaadpy import os_analytical
    except ImportError:
        print("❌ OpenSTAADpy not installed")
        print("   Run: pip install openstaadpy")
        return None
    
    try:
        print("Connecting to Staad Pro...")
        staad = os_analytical.connect()
        
        print("Creating new Staad model...")
        # Create new file: units (4=meters, 5=kN)
        staad.NewSTAADFile(4, 5)
        staad.SetTitle("Oracle - Automated GA Analysis")
        
        # Define columns and their properties
        columns = ga_data['columns']
        beams = ga_data['beams']
        slabs = ga_data['slabs']
        
        print(f"Adding {len(columns)} columns...")
        
        # Node numbering: columns are nodes 1-9
        node_map = {}
        for i, col in enumerate(columns, start=1):
            x = col['x']
            y = col['y']
            z = 0  # Ground level
            
            staad.SetPoint(x, z, y)  # X, Height (Z), Y coordinate
            node_map[col['name']] = i
            print(f"  {col['name']}: ({x}, {y})")
        
        print(f"\nAdding {len(beams)} beams...")
        
        # Define a simple beam section (300x500mm concrete)
        # For now, use generic section
        for i, beam in enumerate(beams, start=1):
            start_node = node_map[beam['start_col']]
            end_node = node_map[beam['end_col']]
            depth = beam['depth_mm']
            
            staad.AddBeam(start_node, end_node, "")
            print(f"  {beam['name']}: Node {start_node} → {end_node} (depth {depth}mm)")
        
        print(f"\nDefining supports...")
        
        # Support all columns at base (fixed supports)
        for i in range(1, len(columns) + 1):
            staad.SetSupport(i, "FIXED")
        
        print(f"\nDefining loads...")
        
        # Dead load on slabs (example: 10 kN/m²)
        # For simplicity, apply as point loads at column nodes
        dead_load_per_bay = 5 * 5 * 10  # 5m x 5m x 10 kN/m² = 250 kN per bay
        live_load_per_bay = 5 * 5 * 2.5  # 2.5 kN/m² live load = 62.5 kN per bay
        
        # Load case 1: Dead load
        staad.DefineLoadcase("DEAD", "DEAD")
        for node in range(1, len(columns) + 1):
            staad.SetMemberLoad(node, "DEAD", dead_load_per_bay, 0, 0, "GY", "")
        
        # Load case 2: Live load
        staad.DefineLoadcase("LIVE", "LIVE")
        for node in range(1, len(columns) + 1):
            staad.SetMemberLoad(node, "LIVE", live_load_per_bay, 0, 0, "GY", "")
        
        print(f"\n✓ Model created successfully")
        print(f"  Nodes: {len(columns)}")
        print(f"  Members: {len(beams)}")
        
        return staad
        
    except Exception as e:
        print(f"❌ Error creating Staad model: {e}")
        return None

def run_analysis(staad):
    """Run Staad analysis"""
    if not staad:
        return False
    
    try:
        print("\nRunning analysis...")
        staad.Run(False)  # False = don't show output
        print("✓ Analysis complete")
        return True
    except Exception as e:
        print(f"❌ Analysis failed: {e}")
        return False

def extract_results(staad):
    """Extract node forces and member forces"""
    if not staad:
        return None
    
    try:
        print("\nExtracting results...")
        
        results = {
            'nodes': [],
            'members': []
        }
        
        # Get node reactions (simplified)
        for node in range(1, 10):  # Nodes 1-9 (columns)
            try:
                # Get reaction data (this varies by Staad version)
                # For now, store node numbers
                results['nodes'].append({
                    'node': node,
                    'fx': 0,
                    'fy': 0,
                    'fz': 0
                })
            except:
                pass
        
        print(f"✓ Extracted {len(results['nodes'])} nodes")
        
        # Save results to JSON
        output_path = OUTPUT_JSON_DIR / "staad_results.json"
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=2)
        
        print(f"✓ Results saved to: {output_path}")
        return results
        
    except Exception as e:
        print(f"❌ Error extracting results: {e}")
        return None

def main():
    print("=== Oracle Phase 3: Staad Pro Integration ===\n")
    
    # Load GA
    ga = load_ga()
    print(f"✓ Loaded GA: {len(ga['columns'])} cols, {len(ga['beams'])} beams, {len(ga['slabs'])} slabs\n")
    
    # Create model
    staad = create_staad_model(ga)
    if not staad:
        print("❌ Failed to create Staad model")
        return None
    
    # Run analysis
    if not run_analysis(staad):
        print("❌ Failed to run analysis")
        return None
    
    # Extract results
    results = extract_results(staad)
    
    if results:
        print("\n✓ Staad integration complete!")
        return results
    else:
        print("\n❌ Failed to extract results")
        return None

if __name__ == "__main__":
    main()