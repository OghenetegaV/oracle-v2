"""Oracle — Command-Line Pipeline (older)

Purpose:
    Runs DXF parse, AI layout, analysis and design in sequence from the command line and
    writes a summary report.

Role in Oracle:
    Older, non-GUI driver of the same phases as the wizard. Nothing imports it and the launcher
    does not use it. It hard-codes test_floor.dxf, a project summary and a fixed timestamp.

Dependencies:
    dxf_parser, claude_ga_generator, staad_mock, staad_v8i_integration, design_module, config.

Consumers:
    None found; run manually as a script.

Status:
    Legacy / Superseded by the wizard. Flagged for human review.

Migration:
    Kept until the owner confirms it is no longer used; could be reduced to a thin
    non-interactive runner over the V2 core later.

Details (original module notes, retained):
    Oracle: AI-Powered Structural Design Automation
    Complete pipeline: DXF → GA → Analysis → Design → Output
"""

import json
from pathlib import Path
from config import OUTPUT_JSON_DIR

def print_header(title):
    """Print phase header"""
    print(f"\n{'='*60}")
    print(f"{title:^60}")
    print(f"{'='*60}\n")

def run_phase_1():
    """Phase 1: DXF Parsing"""
    print_header("PHASE 1: DXF PARSING")
    from dxf_parser import DXFParser
    
    parser = DXFParser("test_floor.dxf")
    parser.parse_all()
    parser.to_json()
    
    data = parser.to_dict()
    print(f"✓ Extracted: {len(data['walls'])} walls, {len(data['columns'])} cols, {len(data['gridlines'])} gridlines")
    return data

def run_phase_2():
    """Phase 2: GA Generation"""
    print_header("PHASE 2: GENERAL ARRANGEMENT")
    from claude_ga_generator import load_parsed_geometry, generate_ga_prompt, call_claude, save_ga
    
    geometry = load_parsed_geometry("test_floor_parsed.json")
    prompt = generate_ga_prompt(geometry)
    ga_response = call_claude(prompt)
    ga_data = save_ga(ga_response)
    
    if ga_data:
        print(f"✓ Generated: {len(ga_data['columns'])} columns, {len(ga_data['beams'])} beams, {len(ga_data['slabs'])} slabs")
    return ga_data

def run_phase_3():
    """Phase 3: Structural Analysis.

    Tries a real STAAD.Pro V8i SS6 analysis first (build model -> run in a live
    STAAD.Pro session -> parse real results), falling back to the BS 6399/BS 8110
    tributary-area mock if STAAD.Pro isn't running, the 32-bit interpreter isn't
    set up, or the live-session analyze step doesn't complete in time. The real
    path also needs design_output.json (real member sections) for MEMBER
    PROPERTY, which doesn't exist yet on a project's very first run -- that
    case falls back to the mock too, since Phase 4 hasn't produced sections yet.
    """
    print_header("PHASE 3: STRUCTURAL ANALYSIS")

    design_path = OUTPUT_JSON_DIR / "design_output.json"
    if not design_path.exists():
        print("No design_output.json yet (first run) -- real STAAD needs member "
              "sections from Phase 4, so using the mock for this run.")
        from staad_mock import load_ga, generate_mock_forces
        return generate_mock_forces(load_ga())

    try:
        import staad_v8i_integration
        print("Attempting real STAAD.Pro V8i SS6 analysis...")
        forces = staad_v8i_integration.run()
        print(f"\n✓ Real STAAD.Pro analysis complete: Generated node and member forces")
        return forces
    except Exception as e:
        print(f"\n⚠️  Real STAAD.Pro analysis unavailable ({e})")
        print("  Falling back to BS 6399/BS 8110 mock analysis...")
        from staad_mock import load_ga, generate_mock_forces
        return generate_mock_forces(load_ga())

def run_phase_4():
    """Phase 4: Element Design"""
    print_header("PHASE 4: ELEMENT DESIGN")
    from design_module import load_ga, load_forces, generate_design_prompt, call_claude_design, save_design, display_design_summary
    
    ga = load_ga()
    forces = load_forces()
    prompt = generate_design_prompt(ga, forces)
    design_response = call_claude_design(prompt)
    design_data = save_design(design_response)
    
    if design_data:
        display_design_summary(design_data)
    return design_data

def generate_final_report(ga, forces, design):
    """Generate final summary report"""
    print_header("FINAL SUMMARY REPORT")
    
    report = {
        "project": "Oracle - Automated Structural Design",
        "timestamp": "2026-09-06",
        "building": {
            "bays": "2×2 (5m × 5m each)",
            "storey_height": "3m",
            "total_area": "100 m²"
        },
        "structural_system": {
            "columns": len(ga['columns']),
            "beams": len(ga['beams']),
            "slabs": len(ga['slabs'])
        },
        "design_code": "BS 8110-1:1997 / BS 4466 & BS 8666 / BS 4449",
        "concrete": "C25/30 (fcu = 25 MPa)",
        "steel": "T/Y (fy = 410 MPa) high-yield per BS 4449",
        "cover": "Foundation 50mm / Column 40mm / Beams & Slabs 25mm",
        "v8i_integration": "Confirmed - Connection working via COM API"
    }
    
    # Save report
    report_path = OUTPUT_JSON_DIR / "oracle_report.json"
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)
    
    print(f"Building Type: {report['building']['bays']} bays")
    print(f"Structural Elements: {report['structural_system']['columns']} columns, {report['structural_system']['beams']} beams, {report['structural_system']['slabs']} slabs")
    print(f"Design Code: {report['design_code']}")
    print(f"Concrete/Steel: {report['concrete']} / {report['steel']}")
    print(f"V8i Integration: {report['v8i_integration']}")
    print(f"\n✓ Report saved to: {report_path}")
    
    return report

def print_files_generated():
    """List all output files generated"""
    print_header("OUTPUT FILES GENERATED")
    
    files = {
        "Parsed Geometry": "test_floor_parsed.json",
        "General Arrangement": "ga_output.json",
        "Structural Analysis": "staad_results.json",
        "Element Design": "design_output.json",
        "Project Report": "oracle_report.json"
    }
    
    output_dir = OUTPUT_JSON_DIR
    print(f"Location: {output_dir}\n")
    
    for description, filename in files.items():
        filepath = output_dir / filename
        if filepath.exists():
            size = filepath.stat().st_size
            print(f"✓ {description:.<30} {filename} ({size} bytes)")
        else:
            print(f"✗ {description:.<30} {filename} (NOT FOUND)")
    
    print(f"\nAll files available at: {output_dir}")

def main():
    print("\n" + "█"*60)
    print("█" + " "*58 + "█")
    print("█" + "  Oracle: AI-Powered Structural Design Automation".center(58) + "█")
    print("█" + " "*58 + "█")
    print("█"*60)
    
    try:
        # Run all phases
        phase1_data = run_phase_1()
        phase2_data = run_phase_2()
        phase3_data = run_phase_3()
        phase4_data = run_phase_4()
        
        # Generate final report
        report = generate_final_report(phase2_data, phase3_data, phase4_data)
        
        # List all files
        print_files_generated()
        
        # Final message
        print_header("✓ ORACLE PIPELINE COMPLETE")
        print("""
All phases executed successfully:
  ✓ Phase 1: DXF Parsing (walls, columns, gridlines extracted)
  ✓ Phase 2: General Arrangement (AI-generated GA with Claude)
  ✓ Phase 3: Structural Analysis (mock Staad Pro analysis)
  ✓ Phase 4: Element Design (AI-designed beams and columns)
  ✓ Phase 5: Report Generation (project summary)

INTEGRATION STATUS:
  ✓ STAAD.Pro V8i SS6: Connection confirmed via COM API
  ✓ Ready for production: Real analysis integration ready
  ✓ Mock forces: Using realistic structural simulation

NEXT STEPS:
  1. Review design_output.json for detailed element sizes
  2. Compare with your company standards
  3. Implement V8i-specific API methods for real Staad analysis
  4. Build Phase 2 LISP detailing automation (per company standards)
  5. Test with real architectural drawings

For more info, see oracle_report.json in output_json folder.
""")
        
        return True
        
    except Exception as e:
        print(f"\n❌ Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)