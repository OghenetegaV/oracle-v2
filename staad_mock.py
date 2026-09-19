"""Oracle — Mock Structural Analysis

Purpose:
    Tributary-area estimate of column reactions and beam forces, with BS 6399-1 loads combined
    per BS 8110-1 cl 2.4.3, written in the same JSON shape as the real STAAD results.

Role in Oracle:
    Legacy fallback analysis, used when STAAD.Pro or the 32-bit interpreter is unavailable.
    The result is an estimate, not a frame analysis.

Dependencies:
    config; company_standards.json.

Consumers:
    oracle_wizard, oracle_pipeline.

Status:
    Legacy / Transitional.

Migration:
    Retained as the no-STAAD fallback; a candidate test double for the analysis adapter.
"""

import json
from pathlib import Path
from config import OUTPUT_JSON_DIR, PROJECT_ROOT

STANDARDS_PATH = PROJECT_ROOT / "company_standards.json"

def load_ga(json_file="ga_output.json"):
    """Load GA from JSON"""
    path = OUTPUT_JSON_DIR / json_file
    with open(path, 'r') as f:
        return json.load(f)

def load_standards(path=STANDARDS_PATH):
    with open(path, 'r') as f:
        return json.load(f)

def factored_udl_kn_m2(ga_data, standards):
    """BS 6399-1 dead + imposed loads, combined per BS 8110-1 cl 2.4.3 (1.4Gk + 1.6Qk)."""
    loading = standards["loading"]
    slabs = ga_data.get("slabs", [])
    slab_thickness_m = (slabs[0]["thickness_mm"] / 1000) if slabs else 0.175

    dead_load_kn_m2 = slab_thickness_m * loading["concrete_unit_weight_kn_m3"] + loading["finishes_kn_m2"]
    imposed_load_kn_m2 = loading["imposed_load_kn_m2"]
    factored = 1.4 * dead_load_kn_m2 + 1.6 * imposed_load_kn_m2

    return factored, dead_load_kn_m2, imposed_load_kn_m2

def average_bay_size_m(ga_data):
    """Representative bay dimensions from the GA grid (assumes a broadly regular grid)."""
    dims = ga_data.get("dimensions", {})
    x_spacings = dims.get("grid_spacing_x_m") or [5]
    y_spacings = dims.get("grid_spacing_y_m") or [5]
    return sum(x_spacings) / len(x_spacings), sum(y_spacings) / len(y_spacings)

def generate_mock_forces(ga_data, standards=None):
    """Generate forces from GA using BS 6399-1 loads and BS 8110 ULS combination
    (mock Staad output -- tributary-area method, gravity loads only)."""

    if standards is None:
        standards = load_standards()

    columns = ga_data['columns']
    beams = ga_data['beams']

    udl_kn_m2, dead_kn_m2, imposed_kn_m2 = factored_udl_kn_m2(ga_data, standards)
    bay_x, bay_y = average_bay_size_m(ga_data)
    bay_area = bay_x * bay_y

    print("Simulating structural analysis...")
    print(f"  Loading (BS 6399-1): Gk={dead_kn_m2:.2f} kN/m^2, Qk={imposed_kn_m2:.2f} kN/m^2")
    print(f"  ULS combination (BS 8110 cl 2.4.3): {udl_kn_m2:.2f} kN/m^2 factored UDL")

    results = {
        'node_forces': {},
        'member_forces': {},
        'loading_basis': {
            'standard': standards['loading']['reference_code'],
            'dead_load_kn_m2': round(dead_kn_m2, 3),
            'imposed_load_kn_m2': imposed_kn_m2,
            'factored_uls_udl_kn_m2': round(udl_kn_m2, 3),
        },
        'analysis_notes': 'Mock tributary-area analysis using BS 6399-1 loads and BS 8110 ULS '
                           'combination -- gravity loads only, no wind/seismic case. Replace with '
                           'real Staad Pro analysis for production use.',
    }

    # Corner columns get 1 bay tributary area (2 directions); edge get 2 bays; interior get 4 bays
    corner_load = (bay_area * udl_kn_m2 / 4) * 4
    edge_load = (bay_area * udl_kn_m2 / 2) * 4
    interior_load = (bay_area * udl_kn_m2) * 4

    xs = [c['x'] for c in columns]
    ys = [c['y'] for c in columns]
    x_min, x_max, y_min, y_max = min(xs), max(xs), min(ys), max(ys)

    for i, col in enumerate(columns, start=1):
        x, y = col['x'], col['y']

        if (x in (x_min, x_max)) and (y in (y_min, y_max)):
            reaction = corner_load
        elif (x in (x_min, x_max)) or (y in (y_min, y_max)):
            reaction = edge_load
        else:
            reaction = interior_load

        results['node_forces'][f"C{i}"] = {
            'node': i,
            'column_name': col['name'],
            'position': [x, y],
            'fy': -reaction,  # Negative = downward
            'fx': 0,
            'fz': 0,
            'mx': 0,
            'my': 0,
            'mz': 0
        }
        print(f"  {col['name']}: {reaction:.1f} kN (vertical, factored ULS)")

    # Beam forces (simplified: span reactions using the same factored UDL)
    print("\nBeam forces (simplified):")
    for i, beam in enumerate(beams, start=1):
        start_col = beam['start_col']
        end_col = beam['end_col']

        span_m = bay_x  # simplification: assumes span aligned to the representative bay size
        beam_load = span_m * (udl_kn_m2 * bay_y / 2)  # tributary half-width

        results['member_forces'][f"B{i}"] = {
            'member': i,
            'beam_name': beam['name'],
            'span_m': span_m,
            'depth_mm': beam['depth_mm'],
            'reaction_start': beam_load / 2,
            'reaction_end': beam_load / 2,
            'max_moment_knm': (beam_load * span_m) / 8,  # simple span formula
            'max_shear_kn': beam_load / 2
        }
        print(f"  {beam['name']}: {beam_load:.1f} kN total, Max M = {results['member_forces'][f'B{i}']['max_moment_knm']:.1f} kNm")

    # Save results
    output_path = OUTPUT_JSON_DIR / "staad_results.json"
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\n✓ Mock analysis complete")
    print(f"✓ Results saved to: {output_path}")

    return results

def main():
    ga = load_ga()
    results = generate_mock_forces(ga)
    return results

if __name__ == "__main__":
    main()
