"""Tests for Oracle's provenance registry and value-status registry (oracle.core, schema 0.2.0)

Protects:
    ProvenanceRecord / SourceReference (creation, field-level records, validation of malformed
    evidence), ValueStatusRecord (the seven statuses, decision backing for engineer values,
    assumed-versus-confirmed), and that both registries survive OracleProject save/load exactly.

Test type:
    Unit tests.

Dependencies:
    oracle.core only.
"""

import tempfile
import unittest
from pathlib import Path

from oracle.core import (
    DecisionCategory, DecisionSource, DecisionStatus, EngineeringDecision, OracleProject, ProvenanceRecord,
    SourceReference, Target, ValidationError, ValueStatus, ValueStatusRecord,
)
from tests.fixtures import make_project

B1, C5, N1, FF = Target.element("B1"), Target.element("C5"), Target.node("N1"), Target.level("FF")


def source(**kw):
    kw.setdefault("file", "GA.dxf")
    kw.setdefault("layer", "F.F BEAMS")
    kw.setdefault("source_id", "member 12")
    return SourceReference(**kw)


_SOURCE_KEYS = {"file", "entity_handle", "layer", "entity_type", "source_id", "coordinates", "coordinate_frame", "context"}


def record(pid="PV-00001", target=B1, field=None, **kw):
    """A ProvenanceRecord; source-reference keyword arguments are routed to its SourceReference."""
    source_kw = {k: kw.pop(k) for k in list(kw) if k in _SOURCE_KEYS}
    kw.setdefault("source", source(**source_kw))
    kw.setdefault("method", "polyline segment")
    kw.setdefault("producer", "ga_dxf_parser")
    return ProvenanceRecord(pid, target, field=field, **kw)


def engineer_decision(did, target, **kw):
    kw.setdefault("status", DecisionStatus.ACCEPTED)
    kw.setdefault("instruction", "Engineer's ruling.")
    return EngineeringDecision(did, "A. Engineer", DecisionSource.ENGINEER, target, DecisionCategory.OTHER, **kw)


class ProvenanceTests(unittest.TestCase):
    def test_create_and_query_object_and_field_level_records(self):
        p = make_project()
        whole = p.add_provenance(record("PV-00001", B1, coordinates=[(0, 0, 3300), (5000, 0, 3300)],
                                        coordinate_frame="sheet mm", confidence=0.9, context="first floor plan"))
        section = p.add_provenance(record("PV-00002", B1, "section", layer=None, source_id="DEFAULT_SIZES"))
        p.add_provenance(record("PV-00003", C5))
        self.assertEqual([r.id for r in p.provenance_for(B1)], ["PV-00001", "PV-00002"])
        self.assertEqual([r.id for r in p.provenance_for(B1, "section")], ["PV-00002"])
        self.assertEqual(p.get_provenance("PV-00003").target, C5)
        self.assertEqual(whole.source.coordinates, ((0.0, 0.0, 3300.0), (5000.0, 0.0, 3300.0)))
        self.assertIsNone(section.source.layer)
        self.assertRegex(whole.recorded_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_records_can_be_about_levels_nodes_and_elements(self):
        p = make_project()
        for i, target in enumerate((FF, N1, C5, B1), start=1):
            p.add_provenance(record(f"PV-{i:05d}", target))
        self.assertEqual(len(p.provenance), 4)

    def test_geometry_and_dotted_fields_are_accepted_but_unknown_ones_are_not(self):
        p = make_project()
        p.add_provenance(record("PV-00001", B1, "geometry"))
        p.add_provenance(record("PV-00002", B1, "section.width_mm"))
        with self.assertRaises(ValidationError):
            p.add_provenance(record("PV-00003", B1, "colour"))          # beams have no such field
        with self.assertRaises(ValidationError):
            record("PV-00003", B1, "Section")                            # not a valid field path

    def test_ids_targets_and_project_scope(self):
        p = make_project()
        p.add_provenance(record("PV-00001"))
        with self.assertRaises(ValidationError):
            p.add_provenance(record("PV-00001"))                         # duplicate id
        with self.assertRaises(ValidationError):
            p.add_provenance(record("PV-00002", Target.element("B404")))  # no such element
        with self.assertRaises(ValidationError):
            record("PV-00002", Target.project())                          # must be about an object
        with self.assertRaises(ValidationError):
            OracleProject.create("No building", "E").add_provenance(record("PV-00001"))

    def test_next_provenance_id_is_sequential(self):
        p = make_project()
        self.assertEqual([p.next_provenance_id() for _ in range(3)], ["PV-00001", "PV-00002", "PV-00003"])

    def test_source_reference_must_identify_evidence(self):
        with self.assertRaises(ValidationError):
            SourceReference()
        with self.assertRaises(ValidationError):
            SourceReference(file="  ")
        SourceReference(context="first floor plan")                       # any one identifier is enough

    def test_source_coordinates_need_a_frame_and_a_valid_shape(self):
        with self.assertRaises(ValidationError):
            SourceReference(file="a.dxf", coordinates=[(1, 2)])            # no frame
        for bad in ([(1,)], [(1, 2, 3, 4)], ["ab"], [(1, float("nan"))], [(1, "2")]):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                SourceReference(file="a.dxf", coordinates=bad, coordinate_frame="mm")

    def test_confidence_and_required_text(self):
        for bad in (-0.1, 1.5, float("nan"), "high"):
            with self.assertRaises(ValidationError, msg=repr(bad)):
                record(confidence=bad)
        record(confidence=0.0)
        record(confidence=1.0)
        for kw in ({"method": " "}, {"producer": ""}, {"recorded_at": "yesterday"}):
            with self.assertRaises(ValidationError, msg=str(kw)):
                record(**kw)

    def test_provenance_survives_save_and_load(self):
        p = make_project()
        p.add_provenance(record("PV-00001", B1, confidence=0.75, note="n", entity_handle="2F3", entity_type="LWPOLYLINE",
                                coordinates=[(1.5, 2.5)], coordinate_frame="mm", context="plan 1"))
        p.add_provenance(record("PV-00002", B1, "section", layer=None, source_id="DEFAULT_SIZES[beam_mm]"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.oracle.json"
            p.save(path)
            q = OracleProject.load(path)
        self.assertEqual([r.to_dict() for r in q.provenance], [r.to_dict() for r in p.provenance])
        first = q.get_provenance("PV-00001")
        self.assertEqual((first.source.entity_handle, first.source.entity_type, first.confidence, first.field),
                         ("2F3", "LWPOLYLINE", 0.75, None))
        self.assertEqual(q.get_provenance("PV-00002").field, "section")
        self.assertEqual(q.next_provenance_id(), "PV-00003")              # the counter resumes after a load
        self.assertEqual(q.to_json(), p.to_json())

    def test_malformed_provenance_is_rejected_on_load(self):
        base = make_project()
        base.add_provenance(record())
        good = base.to_dict()
        def broken(mutate):
            data = OracleProject.from_json(base.to_json()).to_dict()
            mutate(data["provenance"][0])
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(data)
        broken(lambda r: r.pop("method"))
        broken(lambda r: r.update(surprise=1))
        broken(lambda r: r.update(confidence=7))
        broken(lambda r: r.update(field="Bad Field"))
        broken(lambda r: r["target"].update(scope="galaxy"))
        broken(lambda r: r["target"].update(id="B404"))
        broken(lambda r: r["source"].update(coordinates=[[1, 2]], coordinate_frame=None))
        broken(lambda r: r["source"].update(coordinates="not a list"))
        broken(lambda r: r.update(source={"file": None, "layer": None}))
        broken(lambda r: r.update(recorded_at="never"))
        dup = OracleProject.from_json(base.to_json()).to_dict()
        dup["provenance"].append(dict(dup["provenance"][0]))
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(dup)
        OracleProject.from_dict(good)                                     # the untouched dict is still fine


class ValueStatusTests(unittest.TestCase):
    def test_all_seven_statuses_exist_and_round_trip(self):
        self.assertEqual({s.value for s in ValueStatus}, {"source", "inferred", "assumed", "engineer_defined",
                                                          "engineer_override", "calculated", "derived"})
        p = make_project()
        p.add_decision(engineer_decision("D1", B1))
        p.set_value_status(ValueStatusRecord(B1, "geometry", ValueStatus.SOURCE))
        p.set_value_status(ValueStatusRecord(B1, "level_id", ValueStatus.INFERRED))
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED, note="legacy default"))
        p.set_value_status(ValueStatusRecord(B1, "material", ValueStatus.ENGINEER_DEFINED, decision_id="D1"))
        p.set_value_status(ValueStatusRecord(C5, "section", ValueStatus.ENGINEER_OVERRIDE, decision_id="D1",
                                             replaces=ValueStatus.ASSUMED))
        p.set_value_status(ValueStatusRecord(Target.level("FF"), "storey_height_mm", ValueStatus.DERIVED))
        p.set_value_status(ValueStatusRecord(Target.level("GF"), "storey_height_mm", ValueStatus.CALCULATED))
        q = OracleProject.from_json(p.to_json())
        self.assertEqual({r.status for r in q.value_statuses}, set(ValueStatus))
        self.assertEqual(q.value_status_of(C5, "section").replaces, ValueStatus.ASSUMED)
        self.assertEqual(q.to_json(), p.to_json())

    def test_lookup_replace_and_filter(self):
        p = make_project()
        self.assertIsNone(p.value_status_of(B1, "section"))
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED))
        p.set_value_status(ValueStatusRecord(C5, "section", ValueStatus.ASSUMED))
        p.set_value_status(ValueStatusRecord(B1, "geometry", ValueStatus.SOURCE))
        self.assertEqual(len(p.values_with_status(ValueStatus.ASSUMED)), 2)
        self.assertEqual(len(p.values_with_status(ValueStatus.ASSUMED, ValueStatus.SOURCE)), 3)
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.INFERRED))   # one record per (target, field)
        self.assertEqual(p.value_status_of(B1, "section").status, ValueStatus.INFERRED)
        self.assertEqual(len(p.value_statuses), 3)

    def test_engineer_statuses_need_an_accepted_engineer_decision(self):
        with self.assertRaises(ValidationError):
            ValueStatusRecord(B1, "section", ValueStatus.ENGINEER_DEFINED)            # no decision id
        with self.assertRaises(ValidationError):
            ValueStatusRecord(B1, "section", ValueStatus.ASSUMED, decision_id="D1")   # only engineer values carry one
        with self.assertRaises(ValidationError):
            ValueStatusRecord(B1, "section", ValueStatus.SOURCE, replaces=ValueStatus.ASSUMED)
        p = make_project()
        with self.assertRaises(ValidationError):                                        # unknown decision
            p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ENGINEER_DEFINED, decision_id="D404"))
        p.add_decision(EngineeringDecision("R1", "Oracle", DecisionSource.ORACLE, B1, DecisionCategory.SECTION_SIZING,
                                           "Recommend 225x450."))
        with self.assertRaises(ValidationError):                                        # an Oracle decision is not the engineer's
            p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ENGINEER_DEFINED, decision_id="R1"))
        p.add_decision(engineer_decision("D2", B1, status=DecisionStatus.PROPOSED))
        with self.assertRaises(ValidationError):                                        # not accepted yet
            p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ENGINEER_DEFINED, decision_id="D2"))
        p.set_decision_status("D2", DecisionStatus.ACCEPTED)
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ENGINEER_DEFINED, decision_id="D2"))

    def test_status_targets_fields_and_evidence_are_checked(self):
        p = make_project()
        with self.assertRaises(ValidationError):
            p.set_value_status(ValueStatusRecord(Target.element("B404"), "section", ValueStatus.ASSUMED))
        with self.assertRaises(ValidationError):
            p.set_value_status(ValueStatusRecord(B1, "colour", ValueStatus.ASSUMED))
        with self.assertRaises(ValidationError):
            p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED, provenance_ids=["PV-99999"]))
        with self.assertRaises(ValidationError):
            ValueStatusRecord(Target.project(), "name", ValueStatus.SOURCE)
        p.add_provenance(record("PV-00001", B1, "section"))
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED, provenance_ids=["PV-00001"]))
        self.assertEqual(p.value_status_of(B1, "section").provenance_ids, ("PV-00001",))

    def test_decision_about_a_field_must_match_the_status_it_backs(self):
        p = make_project()
        p.set_value(B1, "material", "C30/37", engineer_decision("D1", B1, field="material", value="C30/37"))
        with self.assertRaises(ValidationError):   # the decision is about B1.material, not B1.section
            p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ENGINEER_DEFINED, decision_id="D1"))

    def test_assumed_and_confirmed_values_are_distinguishable_after_reload(self):
        p = make_project()
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED))
        p.set_value_status(ValueStatusRecord(C5, "geometry", ValueStatus.INFERRED))
        p.set_value_status(ValueStatusRecord(N1, "geometry", ValueStatus.SOURCE))
        q = OracleProject.from_json(p.to_json())
        self.assertEqual([r.target.id for r in q.values_with_status(ValueStatus.ASSUMED)], ["B1"])
        self.assertEqual([r.target.id for r in q.values_with_status(ValueStatus.INFERRED)], ["C5"])
        self.assertEqual([r.target.id for r in q.values_with_status(ValueStatus.SOURCE)], ["N1"])

    def test_malformed_value_status_is_rejected_on_load(self):
        p = make_project()
        p.set_value_status(ValueStatusRecord(B1, "section", ValueStatus.ASSUMED))
        def broken(mutate):
            data = OracleProject.from_json(p.to_json()).to_dict()
            mutate(data["value_status"][0])
            with self.assertRaises(ValidationError):
                OracleProject.from_dict(data)
        broken(lambda r: r.update(status="probably"))
        broken(lambda r: r.update(status="engineer_defined"))              # engineer status without a decision
        broken(lambda r: r.update(field="colour"))
        broken(lambda r: r.pop("field"))
        broken(lambda r: r.update(extra=1))
        broken(lambda r: r.update(provenance_ids=["PV-404"]))
        dup = OracleProject.from_json(p.to_json()).to_dict()
        dup["value_status"].append(dict(dup["value_status"][0]))
        with self.assertRaises(ValidationError):
            OracleProject.from_dict(dup)


if __name__ == "__main__":
    unittest.main()
