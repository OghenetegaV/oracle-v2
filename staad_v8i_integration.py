"""Oracle — STAAD.Pro V8i Integration

Purpose:
    Builds STAAD geometry through the OpenSTAAD COM API, patches units, supports, properties
    and loads into the .STD text, runs the analysis in a live STAAD.Pro session and parses the
    .ANL report into staad_results.json.

Role in Oracle:
    Legacy analysis integration and the only real (non-estimate) analysis path. Each COM step
    runs in its own 32-bit Python subprocess because openstaad.dll is 32-bit only.

Dependencies:
    pywin32 and STAAD.Pro V8i SS6 (both only at run time); a 32-bit Python at STAAD_PYTHON32;
    company_standards.json.

Consumers:
    oracle_wizard, oracle_pipeline.

Status:
    Legacy / Integration.

Migration:
    Retained. The working COM findings recorded below must be preserved. To be wrapped as an
    analysis adapter that reads oracle.core objects and returns results into the project.

Details (original module notes, retained):
    Oracle Phase 3 (real): STAAD.Pro V8i SS6 integration via the OpenSTAAD COM API.

    MUST run under a 32-bit Python interpreter -- STAAD.Pro V8i SS6's openstaad.dll
    is 32-bit only. oracle_pipeline.py shells out to one (see STAAD_PYTHON32 below);
    called directly, just run it with that interpreter.

    What this does, and why it's built this way:
      1. Builds real model geometry (nodes, members) via OpenSTAAD.CreateInputOutsideSTAAD
         (ICreateInputOutsideSTAAD) -- verified reliable via COM.
      2. Patches units, supports, member properties, and loads directly into the
         resulting .STD file as STAAD command-language text, instead of using the
         equivalent COM calls (SetInputUnits / multi-node AssignSupportToNode /
         CreateNodalLoad). Those were tested and found to be either silently
         unreliable (only the first of several AssignSupportToNode calls actually
         stuck) or outright unstable (CreateNodalLoad segfaults on several
         nLoadItem values). Since .STD is a plain-text command file, writing the
         documented STAAD syntax directly is both safe and standard practice.
      3. Applies the slab load as a real MEMBER LOAD UDL on each beam (BS 6399-1
         dead + imposed, combined per BS 8110 cl 2.4.3 as 1.4Gk+1.6Qk), so beam
         moments/shears come from actual frame analysis under a real distributed
         load -- not from a pre-computed point load at the columns. Column axial
         reactions emerge from the analysis itself.
      4. Opens the model in a live, already-running STAAD.Pro session and runs the
         analysis (OpenSTAAD.CreateInputOutsideSTAAD.RunSTAADEngine was tested and
         does not reliably produce output standalone -- the GUI-session path is
         the one that's actually been verified to work end-to-end).
      5. Polls OpenSTAAD.Output.AreResultsAvailable (reliable) rather than trusting
         GetSTAADFile()/Analyze()'s return values (both proved unreliable as state
         indicators in this version).
      6. Parses the real .ANL text report STAAD writes and returns results in the
         same shape as staad_mock.py's output, so design_module.py needs no changes.

    Precondition: STAAD.Pro V8i SS6 must already be running (any file/no file open
    is fine). If it isn't, this raises RuntimeError so the caller can fall back to
    staad_mock.py.
"""

import json
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
OUTPUT_JSON_DIR = PROJECT_ROOT / "output_json"
OUTPUT_STAAD_DIR = PROJECT_ROOT / "output_staad"
OUTPUT_STAAD_DIR.mkdir(parents=True, exist_ok=True)

STD_PATH = OUTPUT_STAAD_DIR / "oracle_model.std"
ANL_PATH = OUTPUT_STAAD_DIR / "oracle_model.ANL"
MEMBER_MAP_PATH = OUTPUT_STAAD_DIR / "_member_map.json"

CREATEINPUT_TYPELIB_GUID = "{7B60A3A6-8760-461F-B3C1-E5C4FE1B8F7D}"
RESULTS_POLL_TIMEOUT_S = 180  # GUI-based open+parse+analyze has real rendering overhead, not just solve time
RESULTS_POLL_INTERVAL_S = 2

# Dedicated 32-bit interpreter -- STAAD.Pro V8i SS6's openstaad.dll is 32-bit only.
STAAD_PYTHON32 = r"C:\Python311-32\python.exe"


def python32_executable():
    """The interpreter to use for COM-touching subprocess steps: reuse the
    current interpreter if it's already 32-bit, else the dedicated one."""
    if struct.calcsize("P") * 8 == 32:
        return sys.executable
    if not Path(STAAD_PYTHON32).exists():
        raise RuntimeError(
            f"No 32-bit Python found at {STAAD_PYTHON32} (needed for STAAD.Pro V8i "
            "SS6's 32-bit-only openstaad.dll) and the current interpreter is 64-bit."
        )
    return STAAD_PYTHON32


def require_32bit():
    if struct.calcsize("P") * 8 != 32:
        raise RuntimeError(
            "staad_v8i_integration.py must run under a 32-bit Python interpreter "
            "(STAAD.Pro V8i SS6's openstaad.dll is 32-bit only). "
            "Run with the dedicated interpreter, e.g.:\n"
            r'  C:\Python311-32\python.exe staad_v8i_integration.py'
        )


def load_json(path):
    with open(path) as f:
        return json.load(f)


def section_to_yd_zd_m(section_str):
    w, d = (int(x) for x in re.findall(r"\d+", section_str))
    return round(w / 1000, 4), round(d / 1000, 4)


def factored_slab_udl_kn_m2(ga, standards):
    """BS 6399-1 dead + imposed loads, combined per BS 8110 cl 2.4.3 (1.4Gk+1.6Qk)."""
    loading = standards["loading"]
    slabs = ga.get("slabs", [])
    slab_thickness_m = (slabs[0]["thickness_mm"] / 1000) if slabs else 0.175
    dead_kn_m2 = slab_thickness_m * loading["concrete_unit_weight_kn_m3"] + loading["finishes_kn_m2"]
    imposed_kn_m2 = loading["imposed_load_kn_m2"]
    return 1.4 * dead_kn_m2 + 1.6 * imposed_kn_m2


def average_bay_size_m(ga):
    dims = ga.get("dimensions", {})
    x_spacings = dims.get("grid_spacing_x_m") or [5]
    y_spacings = dims.get("grid_spacing_y_m") or [5]
    return sum(x_spacings) / len(x_spacings), sum(y_spacings) / len(y_spacings)


def to_ranges(numbers):
    """[10,11,12,13] -> '10 TO 13'; non-contiguous falls back to space-separated."""
    numbers = sorted(numbers)
    if numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"{numbers[0]} TO {numbers[-1]}"
    return " ".join(str(n) for n in numbers)


def build_geometry_com(ga):
    """COM-only step: build node/member geometry and write the raw .STD shell.
    Must run as its own short-lived process -- see the module docstring's note
    on this DLL's instability when COM calls and later file I/O share a process."""
    import win32com.client
    import win32com.client.gencache as gc

    # If STD_PATH is still open in a live STAAD.Pro session (e.g. from a prior
    # run), WriteSTAADFile crashes outright on the locked file instead of
    # erroring cleanly. Best-effort close first; harmless if nothing's open,
    # and skipped entirely if STAAD.Pro isn't running (that's run_analysis()'s
    # problem to raise clearly, not this step's).
    try:
        main = win32com.client.GetObject(Class="StaadPro.OpenSTAAD")
        main.CloseSTAADFile()
    except Exception:
        pass

    mod = gc.EnsureModule(CREATEINPUT_TYPELIB_GUID, 0, 1, 0)
    raw = win32com.client.Dispatch("OpenSTAAD.CreateInputOutsideSTAAD")
    ci = mod.ICreateInputOutsideSTAAD(raw._oleobj_)

    storey_h = ga.get("dimensions", {}).get("storey_height_m", 3.0)
    col_names = [c["name"] for c in ga["columns"]]
    node_of = {name: i + 1 for i, name in enumerate(col_names)}
    n_base = len(col_names)

    ci.CreateSTDFileShell()
    ci.SetSTAADStructType(0)  # SPACE

    for c in ga["columns"]:
        i = node_of[c["name"]]
        ci.AddNode(i, c["x"], 0.0, c["y"])          # base
        ci.AddNode(i + n_base, c["x"], storey_h, c["y"])  # top

    members = []
    beam_no = 1
    for c in ga["columns"]:
        i = node_of[c["name"]]
        ci.AddBeam(beam_no, i, i + n_base, 0.0)
        members.append((beam_no, "COLUMN", c["name"]))
        beam_no += 1
    for b in ga["beams"]:
        a = node_of[b["start_col"]] + n_base
        z = node_of[b["end_col"]] + n_base
        ci.AddBeam(beam_no, a, z, 0.0)
        members.append((beam_no, "BEAM", b["name"]))
        beam_no += 1

    # Only one AssignSupportToNode call: repeated calls to this method have been
    # observed to intermittently crash the DLL, and patch_std_text() below fully
    # overwrites the SUPPORTS block anyway, so a single call (satisfying whatever
    # internal STAAD.Pro requirement exists for "at least one support" at write
    # time) is both sufficient and safer than looping.
    ci.CreateSupport(1, 1, 0, 0.0, 0)
    ci.AssignSupportToNode(node_of[ga["columns"][0]["name"]], 1)

    ci.WriteSTAADFile(str(STD_PATH))

    member_map = {"node_of": node_of, "n_base": n_base, "members": members, "storey_h": storey_h}
    MEMBER_MAP_PATH.write_text(json.dumps(member_map, indent=2))
    return member_map


def patch_std_text(ga, design, standards, member_map):
    """Pure-Python step (no COM): patch units/supports/properties/loads into the
    .STD file as STAAD command-language text. Safe to run in any process/interpreter."""
    node_of = member_map["node_of"]
    members = member_map["members"]

    col_section = {c["name"]: c["section"] for c in design["columns"]}
    beam_section = {b["name"]: b["section"] for b in design["beams"]}

    prop_lines = []
    for member_no, kind, name in members:
        section = col_section[name] if kind == "COLUMN" else beam_section[name]
        yd, zd = section_to_yd_zd_m(section)
        prop_lines.append(f"{member_no} PRIS YD {yd} ZD {zd}")

    base_nodes = sorted(node_of.values())
    supports_line = to_ranges(base_nodes) + " FIXED"

    udl_kn_m2 = factored_slab_udl_kn_m2(ga, standards)
    _, bay_y = average_bay_size_m(ga)
    udl_per_length_kn_m = round(udl_kn_m2 * bay_y / 2, 3)

    beam_member_nos = [m[0] for m in members if m[1] == "BEAM"]
    beam_range = to_ranges(beam_member_nos)

    text = STD_PATH.read_text()
    text = re.sub(r"Unit .+", "UNIT METER KN", text)
    text = re.sub(r"CONSTANTS\nSUPPORTS", "SUPPORTS", text)  # drop stray empty CONSTANTS if present
    text = re.sub(
        r"SUPPORTS\n.*?\nMEMBER PROPERTY\n",
        f"SUPPORTS\n{supports_line}\nMEMBER PROPERTY\n" + "\n".join(prop_lines) + "\n",
        text, flags=re.DOTALL,
    )
    text = re.sub(
        r"PERFORM ANALYSIS.*$",
        "CONSTANTS\nE CONCRETE ALL\nDENSITY CONCRETE ALL\nPOISSON CONCRETE ALL\n"
        f"LOAD 1 ORACLE SLAB DEAD+IMPOSED (BS 6399-1, BS 8110 ULS 1.4Gk+1.6Qk)\n"
        f"MEMBER LOAD\n{beam_range} UNI GY {-udl_per_length_kn_m}\n"
        "PERFORM ANALYSIS\nPRINT SUPPORT REACTIONS\nPRINT MEMBER FORCES\nFINISH\n",
        text, flags=re.DOTALL,
    )
    STD_PATH.write_text(text)

    member_map = dict(member_map, udl_per_length_kn_m=udl_per_length_kn_m)
    MEMBER_MAP_PATH.write_text(json.dumps(member_map, indent=2))
    return member_map


ANALYSIS_DONE_MARKER = "END OF LATEST ANALYSIS RESULT"


def run_analysis():
    """Open the model in a live STAAD.Pro session and run the analysis.
    Requires STAAD.Pro V8i SS6 to already be running.

    Polls the .ANL report file directly for its completion marker rather than
    OpenSTAAD.Output.AreResultsAvailable(): that call was tested against a
    model that had genuinely and correctly finished analysing (verified by
    reading the .ANL directly) and still reported no results available --
    it's unreliable in this DLL version, same as GetSTAADFile()/Analyze()'s
    return values. The .ANL file and its marker text are what STAAD.Pro
    itself writes on completion, so they're the actual ground truth."""
    import win32com.client

    try:
        main = win32com.client.GetObject(Class="StaadPro.OpenSTAAD")
    except Exception as e:
        raise RuntimeError(
            "Could not attach to a running STAAD.Pro V8i SS6 instance -- "
            "make sure STAAD.Pro is open."
        ) from e

    # Clear any stale report from a previous run so its old completion marker
    # can't produce a false positive below.
    if ANL_PATH.exists():
        ANL_PATH.unlink()

    main.OpenSTAADFile(str(STD_PATH))
    main.Analyze()

    waited = 0
    while waited < RESULTS_POLL_TIMEOUT_S:
        if ANL_PATH.exists():
            try:
                if ANALYSIS_DONE_MARKER in ANL_PATH.read_text(errors="ignore"):
                    return
            except OSError:
                pass  # file mid-write; retry next tick
        time.sleep(RESULTS_POLL_INTERVAL_S)
        waited += RESULTS_POLL_INTERVAL_S

    raise RuntimeError(
        f"No '{ANALYSIS_DONE_MARKER}' in {ANL_PATH.name} after {RESULTS_POLL_TIMEOUT_S}s "
        "-- the model may have failed to load/analyze in STAAD.Pro. Check the STAAD.Pro window."
    )


REACTION_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$"
)
MEMBER_LINE_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$"
)
MEMBER_CONT_RE = re.compile(
    r"^\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$"
)


def parse_reactions(text, base_node_to_col):
    in_table = False
    node_forces = {}
    for line in text.splitlines():
        if "JOINT  LOAD   FORCE-X" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if "END OF LATEST ANALYSIS" in line:
            break
        m = REACTION_RE.match(line)
        if m:
            joint, lc, fx, fy, fz, mx, my, mz = m.groups()
            col_name = base_node_to_col.get(int(joint))
            if col_name:
                node_forces[col_name] = {
                    "node": int(joint), "column_name": col_name,
                    "fx": float(fx), "fy": float(fy), "fz": float(fz),
                    "mx": float(mx), "my": float(my), "mz": float(mz),
                }
    return node_forces


def parse_member_forces(text, member_no_to_name, member_no_to_kind, beam_meta):
    member_forces = {}
    in_table = False
    current_member = None
    current_rows = []
    for line in text.splitlines():
        if "MEMBER  LOAD  JT     AXIAL" in line:
            in_table = True
            continue
        if not in_table:
            continue
        if "END OF LATEST ANALYSIS" in line:
            break
        m = MEMBER_LINE_RE.match(line)
        if m:
            current_member = int(m.group(1))
            current_rows = [tuple(float(x) for x in m.groups()[3:9])]
            continue
        m2 = MEMBER_CONT_RE.match(line)
        if m2 and current_member is not None:
            current_rows.append(tuple(float(x) for x in m2.groups()[1:7]))
            name = member_no_to_name.get(current_member)
            if name and member_no_to_kind.get(current_member) == "BEAM":
                axial = max(abs(r[0]) for r in current_rows)
                shear = max(abs(r[1]) for r in current_rows)
                moment = max(abs(r[5]) for r in current_rows)
                meta = beam_meta.get(name, {})
                member_forces[name] = {
                    "member": current_member, "beam_name": name,
                    "span_m": meta.get("span_m"), "depth_mm": meta.get("depth_mm"),
                    "max_axial_kn": axial, "max_shear_kn": shear, "max_moment_knm": moment,
                }
            current_member = None
    return member_forces


def parse_results(ga, member_map, udl_kn_m2):
    base_node_to_col = {v: k for k, v in member_map["node_of"].items()}
    member_no_to_name = {m[0]: m[2] for m in member_map["members"]}
    member_no_to_kind = {m[0]: m[1] for m in member_map["members"]}

    col_positions = {c["name"]: (c["x"], c["y"]) for c in ga["columns"]}

    def span_m(b):
        (x1, y1), (x2, y2) = col_positions[b["start_col"]], col_positions[b["end_col"]]
        return round(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5, 3)

    beam_meta = {b["name"]: {"span_m": span_m(b), "depth_mm": b["depth_mm"]} for b in ga["beams"]}

    text = ANL_PATH.read_text()
    node_forces = parse_reactions(text, base_node_to_col)
    member_forces = parse_member_forces(text, member_no_to_name, member_no_to_kind, beam_meta)

    return {
        "node_forces": node_forces,
        "member_forces": member_forces,
        "loading_basis": {
            "standard": "BS 6399-1:1996 / BS 8110-1:1997 cl 2.4.3",
            "factored_uls_udl_kn_m2": round(udl_kn_m2, 3),
            "source": "Real STAAD.Pro V8i SS6 analysis via OpenSTAAD COM API (not mock)",
        },
        "analysis_notes": "Real STAAD.Pro V8i SS6 analysis: slab load applied as MEMBER LOAD UDL "
                           "on beams (BS 6399-1/BS 8110), column reactions and beam forces both "
                           "emerge from actual frame analysis, parsed from oracle_model.ANL.",
    }


def run_subprocess_step(step):
    """Re-invoke this script for one COM-touching step, in a fresh process.
    Each step (geometry build, live-session analyze) has been found to be
    reliable in isolation but unstable when chained together in one process
    -- see the module docstring. sys.executable ensures the same (32-bit)
    interpreter is reused."""
    result = subprocess.run(
        [python32_executable(), str(Path(__file__).resolve()), "--step", step],
        capture_output=True, text=True, timeout=RESULTS_POLL_TIMEOUT_S + 30,
    )
    print(result.stdout, end="")
    if result.returncode != 0:
        raise RuntimeError(f"Step '{step}' failed (exit {result.returncode}):\n{result.stderr}")


def run():
    """Orchestrator: safe to call from a normal (any-bitness) Python process.
    Delegates each COM-touching step to its own 32-bit subprocess."""
    ga = load_json(OUTPUT_JSON_DIR / "ga_output.json")
    design = load_json(OUTPUT_JSON_DIR / "design_output.json")
    standards = load_json(PROJECT_ROOT / "company_standards.json")

    print("Building real STAAD geometry via COM (subprocess)...")
    run_subprocess_step("geometry")
    member_map = json.loads(MEMBER_MAP_PATH.read_text())

    print("Patching units/supports/properties/slab loads (no COM)...")
    member_map = patch_std_text(ga, design, standards, member_map)
    udl_kn_m2 = factored_slab_udl_kn_m2(ga, standards)
    print(f"  Slab UDL on beams: {member_map['udl_per_length_kn_m']} kN/m "
          f"(from {udl_kn_m2:.2f} kN/m^2 factored ULS)")

    print("Running analysis in live STAAD.Pro session (subprocess)...")
    run_subprocess_step("analyze")

    print("Parsing real results (no COM)...")
    results = parse_results(ga, member_map, udl_kn_m2)

    out_path = OUTPUT_JSON_DIR / "staad_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"Saved real results to: {out_path}")
    for name, nf in results["node_forces"].items():
        print(f"  {name}: FY={nf['fy']:.2f} kN")
    return results


if __name__ == "__main__":
    if "--step" in sys.argv:
        require_32bit()
        step = sys.argv[sys.argv.index("--step") + 1]
        if step == "geometry":
            _ga = load_json(OUTPUT_JSON_DIR / "ga_output.json")
            build_geometry_com(_ga)
        elif step == "analyze":
            run_analysis()
        else:
            raise SystemExit(f"Unknown step: {step}")
    else:
        run()
