#!/usr/bin/env python3
"""
Unit and Integration Tests for SPCC Overdue-Plan Trigger Scanner
===============================================================
Tests all core domain rules:
1. Capacity bucketing under 40 CFR 112 (Exempt, Tier I Qualified, Tier II Qualified, PE-Certified, Large Terminal)
2. Overdue-date math (5-year mandatory review cycle under 40 CFR 112.5(b), 6-month material modification window under 40 CFR 112.5(a))
3. Heuristic scoring engine (multi-factor priority scoring, priority level categorization)
4. Adapter schema normalization (WVDEP AstAll, NHDES AST, MN PCA, EPA ECHO)
5. Spatial utilities and distance calculation
6. LeadExporter (CSV & JSON serialization)

*NOTE*: All tests use synthetic fixture rows and mock objects. No live network required.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

# Ensure local module directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scanner import (
    THRESHOLD_SPCC_MIN_GAL,
    THRESHOLD_QUALIFIED_MAX_GAL,
    THRESHOLD_TIER_I_MAX_INDIVIDUAL_GAL,
    THRESHOLD_LARGE_TERMINAL_GAL,
    PLAN_PRICING,
    LEAD_SUBSCRIPTION_BENCHMARKS,
    NormalizedTankFacility,
    ProspectEvaluation,
    RailStatus,
    OverdueScoringEngine,
    LeadExporter,
    WVDEPAstRail,
    NHDESAstRail,
    MNPCAstRail,
    ECHORestRail,
    haversine_distance_km
)


class TestCapacityBucketing(unittest.TestCase):
    def test_sub_threshold_exempt(self):
        # < 1,320 gal is exempt under 40 CFR 112.1(d)(2)
        tier, p_min, p_mid, p_max = OverdueScoringEngine.classify_capacity_tier(550.0, 550.0)
        self.assertEqual(tier, "EXEMPT")
        self.assertEqual(p_min, 0.0)
        self.assertEqual(p_mid, 0.0)

        # 1,319 gal boundary
        tier_edge, _, _, _ = OverdueScoringEngine.classify_capacity_tier(1319.9, 1319.9)
        self.assertEqual(tier_edge, "EXEMPT")

    def test_tier_i_qualified_facility(self):
        # 1,320 <= agg <= 10,000 AND max single container <= 5,000 gal (40 CFR 112.3(g)(1))
        tier, p_min, p_mid, p_max = OverdueScoringEngine.classify_capacity_tier(5000.0, 2500.0)
        self.assertEqual(tier, "TIER_I_QUALIFIED")
        self.assertEqual(p_min, PLAN_PRICING["TIER_I_QUALIFIED"]["min"])
        self.assertEqual(p_mid, PLAN_PRICING["TIER_I_QUALIFIED"]["mid"])
        self.assertEqual(p_max, PLAN_PRICING["TIER_I_QUALIFIED"]["max"])

        # Exact boundary: 1,320 gal agg, 1,320 gal max
        tier_boundary, _, _, _ = OverdueScoringEngine.classify_capacity_tier(1320.0, 1320.0)
        self.assertEqual(tier_boundary, "TIER_I_QUALIFIED")

        # Boundary: 10,000 gal agg, 5,000 gal max
        tier_boundary_max, _, _, _ = OverdueScoringEngine.classify_capacity_tier(10000.0, 5000.0)
        self.assertEqual(tier_boundary_max, "TIER_I_QUALIFIED")

    def test_tier_ii_qualified_facility(self):
        # 1,320 <= agg <= 10,000 BUT max single container > 5,000 gal (40 CFR 112.3(g)(2))
        tier, p_min, p_mid, p_max = OverdueScoringEngine.classify_capacity_tier(8000.0, 6000.0)
        self.assertEqual(tier, "TIER_II_QUALIFIED")
        self.assertEqual(p_min, PLAN_PRICING["TIER_II_QUALIFIED"]["min"])
        self.assertEqual(p_mid, PLAN_PRICING["TIER_II_QUALIFIED"]["mid"])
        self.assertEqual(p_max, PLAN_PRICING["TIER_II_QUALIFIED"]["max"])

        # Boundary: 5,001 gal max tank
        tier_boundary, _, _, _ = OverdueScoringEngine.classify_capacity_tier(9000.0, 5001.0)
        self.assertEqual(tier_boundary, "TIER_II_QUALIFIED")

    def test_pe_certified_standard_tier(self):
        # 10,001 to 250,000 gal -> Full PE certification required under 40 CFR 112.3(d)
        tier, p_min, p_mid, p_max = OverdueScoringEngine.classify_capacity_tier(50000.0, 15000.0)
        self.assertEqual(tier, "PE_CERTIFIED_STANDARD")
        self.assertEqual(p_min, 5000.0)
        self.assertEqual(p_mid, 6500.0)
        self.assertEqual(p_max, 8000.0)

        # Boundary: 10,001 gal
        tier_edge, _, _, _ = OverdueScoringEngine.classify_capacity_tier(10001.0, 5000.0)
        self.assertEqual(tier_edge, "PE_CERTIFIED_STANDARD")

    def test_pe_certified_large_terminal_tier(self):
        # > 250,000 gal -> Complex multi-tank terminal / high hazard
        tier, p_min, p_mid, p_max = OverdueScoringEngine.classify_capacity_tier(500000.0, 100000.0)
        self.assertEqual(tier, "PE_CERTIFIED_LARGE")
        self.assertEqual(p_min, 8000.0)
        self.assertEqual(p_mid, 11500.0)
        self.assertEqual(p_max, 16000.0)


class TestOverdueDateMath(unittest.TestCase):
    def setUp(self):
        self.ref_date = datetime(2026, 11, 2, tzinfo=timezone.utc)

    def test_5yr_review_cycle_overdue(self):
        # Vintage 2018 vs 2026 -> 8 years elapsed (3 years overdue)
        years, is_overdue, material_mod = OverdueScoringEngine.evaluate_vintage_and_overdue_math(
            inferred_vintage_year=2018,
            latest_tank_action_date="2018-05-15",
            reference_date=self.ref_date
        )
        self.assertEqual(years, 8.0)
        self.assertTrue(is_overdue)
        self.assertTrue(material_mod)  # 2018 action is > 180 days ago

    def test_5yr_review_cycle_current(self):
        # Vintage 2023 vs 2026 -> 3 years elapsed (plan is current, not overdue)
        years, is_overdue, material_mod = OverdueScoringEngine.evaluate_vintage_and_overdue_math(
            inferred_vintage_year=2023,
            latest_tank_action_date="2026-09-01",  # Recent addition (< 180 days)
            reference_date=self.ref_date
        )
        self.assertEqual(years, 3.0)
        self.assertFalse(is_overdue)
        self.assertFalse(material_mod)  # September 2026 to Nov 2026 is ~62 days (< 180 days)

    def test_exact_5yr_boundary(self):
        # Vintage 2021 vs 2026 -> exactly 5.0 years -> triggers overdue
        years, is_overdue, _ = OverdueScoringEngine.evaluate_vintage_and_overdue_math(
            inferred_vintage_year=2021,
            latest_tank_action_date=None,
            reference_date=self.ref_date
        )
        self.assertEqual(years, 5.0)
        self.assertTrue(is_overdue)

    def test_missing_vintage_fallback(self):
        # Untracked active tank defaults to conservative 6.0 years elapsed (overdue)
        years, is_overdue, _ = OverdueScoringEngine.evaluate_vintage_and_overdue_math(
            inferred_vintage_year=None,
            latest_tank_action_date=None,
            reference_date=self.ref_date
        )
        self.assertEqual(years, 6.0)
        self.assertTrue(is_overdue)


class TestScoringEngine(unittest.TestCase):
    def setUp(self):
        self.ref_date = datetime(2026, 11, 2, tzinfo=timezone.utc)

    def test_critical_priority_prospect(self):
        # Large chemical terminal, vintage 2014, near river intake, violation history
        fac = NormalizedTankFacility(
            facility_id="TEST-CRIT-01",
            name="Alpha Chemical Works",
            owner="Alpha Industries",
            street="100 River Rd",
            city="Nitro",
            state="WV",
            county="Kanawha",
            latitude=38.41,
            longitude=-81.82,
            tank_count=10,
            aggregate_capacity_gal=350000.0,
            max_individual_tank_gal=80000.0,
            substances=["Petroleum Distillates", "Solvents"],
            inferred_vintage_year=2014,
            latest_tank_action_date="2016-01-01",
            last_inspection_date="05/10/2015",
            violation_flag=True,
            violation_details="ECHO Significant Non-Compliance Flagged",
            distance_to_waterway_km=0.15,
            waterway_name="Kanawha River",
            surface_water_intake_nearby=True,
            source_rail="TestRail",
            raw_record_ref="Ref:01"
        )
        eval_res = OverdueScoringEngine.score_facility(fac, reference_date=self.ref_date)
        self.assertEqual(eval_res.priority_level, "CRITICAL")
        self.assertGreaterEqual(eval_res.overdue_score, 75.0)
        self.assertTrue(eval_res.is_overdue_5yr)
        self.assertTrue(eval_res.material_modification_trigger)
        self.assertIn("PE-Certified", eval_res.capacity_tier)
        self.assertGreaterEqual(eval_res.estimated_plan_value_mid, 10000.0)

    def test_exempt_facility_scoring(self):
        # Small shop with 500 gallon waste oil tank
        fac = NormalizedTankFacility(
            facility_id="TEST-EXEMPT-01",
            name="Corner Auto Lube",
            owner="Corner Lube LLC",
            street="5 Main St",
            city="Charleston",
            state="WV",
            county="Kanawha",
            latitude=38.35,
            longitude=-81.63,
            tank_count=1,
            aggregate_capacity_gal=500.0,
            max_individual_tank_gal=500.0,
            substances=["Waste Oil"],
            inferred_vintage_year=2015,
            latest_tank_action_date=None,
            last_inspection_date=None,
            violation_flag=False,
            violation_details="None",
            distance_to_waterway_km=2.0,
            waterway_name=None,
            surface_water_intake_nearby=False,
            source_rail="TestRail",
            raw_record_ref="Ref:02"
        )
        eval_res = OverdueScoringEngine.score_facility(fac, reference_date=self.ref_date)
        self.assertEqual(eval_res.priority_level, "EXEMPT")
        self.assertEqual(eval_res.overdue_score, 0.0)
        self.assertEqual(eval_res.estimated_plan_value_mid, 0.0)

    def test_ranking_sort_order(self):
        # Higher score must rank above lower score; ties broken by capacity
        p1 = ProspectEvaluation(
            facility_id="P1", name="Low Score", owner="O1", street="", city="", state="WV", county="",
            latitude=None, longitude=None, tank_count=2, aggregate_capacity_gal=50000.0, max_individual_tank_gal=25000.0,
            capacity_tier="PE-Certified", estimated_plan_value_min=5000.0, estimated_plan_value_mid=6500.0, estimated_plan_value_max=8000.0,
            inferred_plan_vintage=2023, years_since_vintage=3.0, is_overdue_5yr=False, material_modification_trigger=False,
            inspection_gap_years=None, violation_flag=False, distance_to_waterway_km=None, waterway_name=None,
            overdue_score=40.0, priority_level="MODERATE", primary_trigger_reason="Trigger", all_trigger_reasons=[],
            recommended_consultant_action="Act", monthly_lead_tier="$1,200/mo", source_rail=""
        )
        p2 = ProspectEvaluation(
            facility_id="P2", name="High Score", owner="O2", street="", city="", state="WV", county="",
            latitude=None, longitude=None, tank_count=5, aggregate_capacity_gal=100000.0, max_individual_tank_gal=50000.0,
            capacity_tier="PE-Certified", estimated_plan_value_min=5000.0, estimated_plan_value_mid=6500.0, estimated_plan_value_max=8000.0,
            inferred_plan_vintage=2015, years_since_vintage=11.0, is_overdue_5yr=True, material_modification_trigger=True,
            inspection_gap_years=None, violation_flag=False, distance_to_waterway_km=None, waterway_name=None,
            overdue_score=95.0, priority_level="CRITICAL", primary_trigger_reason="Trigger", all_trigger_reasons=[],
            recommended_consultant_action="Act", monthly_lead_tier="$1,200/mo", source_rail=""
        )
        ranked = OverdueScoringEngine.rank_prospects([p1, p2])
        self.assertEqual(ranked[0].facility_id, "P2")
        self.assertEqual(ranked[1].facility_id, "P1")


class TestSchemaNormalization(unittest.TestCase):
    def test_wvdep_normalization(self):
        adapter = WVDEPAstRail()
        raw_tanks = [
            {
                "fid": 101,
                "permitid": "2018-0009999",
                "owner_name": "TEST ENERGY CORP",
                "county": "Kanawha",
                "latitude": 38.38,
                "longitude": -81.75,
                "total_capa": 12000.0,
                "substances": "Diesel",
                "pubsw_wint": "CHARLESTON WATER",
                "zcc": "In",
                "zpc": "In"
            },
            {
                "fid": 102,
                "permitid": "2018-0009999",
                "owner_name": "TEST ENERGY CORP",
                "county": "Kanawha",
                "latitude": 38.38,
                "longitude": -81.75,
                "total_capa": 8000.0,
                "substances": "Lube Oil",
                "pubsw_wint": "CHARLESTON WATER",
                "zcc": "In",
                "zpc": "In"
            }
        ]
        fac = adapter._normalize_wvdep_facility("2018-0009999", raw_tanks)
        self.assertIsNotNone(fac)
        self.assertEqual(fac.facility_id, "WVDEP-2018-0009999")
        self.assertEqual(fac.tank_count, 2)
        self.assertEqual(fac.aggregate_capacity_gal, 20000.0)
        self.assertEqual(fac.max_individual_tank_gal, 12000.0)
        self.assertEqual(fac.inferred_vintage_year, 2018)
        self.assertTrue(fac.surface_water_intake_nearby)

    def test_nhdes_normalization(self):
        adapter = NHDESAstRail()
        raw_rows = [
            {
                "OBJECTID": 999,
                "SITE_NO": 198700123,
                "FAC_NO": "870123A",
                "FACILITY": "GRANITE STATE PAPER MILL",
                "ADDRESS": "50 MILL RD",
                "TOWN": "BERLIN",
                "FAC_TYPE": "INDUSTRIAL",
                "LONGITUDE": -71.18,
                "LATITUDE": 44.47,
                "ONESTOP_LINK": "https://onestop.des.nh.gov"
            }
        ]
        fac = adapter._normalize_nhdes_facility("870123A", raw_rows)
        self.assertIsNotNone(fac)
        self.assertEqual(fac.facility_id, "NHDES-870123A")
        self.assertEqual(fac.name, "GRANITE STATE PAPER MILL")
        self.assertEqual(fac.city, "BERLIN")
        self.assertEqual(fac.inferred_vintage_year, 1987)
        self.assertGreater(fac.aggregate_capacity_gal, 10000.0)

    def test_mnpca_normalization(self):
        adapter = MNPCAstRail()
        raw_tanks = [
            {
                "OBJECTID": 501,
                "ai_id": 12345,
                "ai_name": "NORTH STAR REFINING",
                "address1": "100 Refinery Rd",
                "city": "St. Paul",
                "county": "Ramsey",
                "huc8_name": "Mississippi River - Twin Cities",
                "capacity": 85000.0,
                "substance": "Heavy Fuel Oil",
                "install_date": 1420070400000,  # 2015-01-01 epoch ms
                "latitude": 44.95,
                "longitude": -93.09
            }
        ]
        fac = adapter._normalize_mnpca_facility("12345", raw_tanks)
        self.assertIsNotNone(fac)
        self.assertEqual(fac.facility_id, "MNPCA-12345")
        self.assertEqual(fac.name, "NORTH STAR REFINING")
        self.assertEqual(fac.aggregate_capacity_gal, 85000.0)
        self.assertEqual(fac.inferred_vintage_year, 2015)
        self.assertIn("Mississippi", fac.waterway_name)

    def test_echo_normalization(self):
        adapter = ECHORestRail(state_filter="WV")
        raw_f = {
            "RegistryID": "110000998877",
            "FacName": "MOUNTAIN STATE FUEL CO",
            "FacStreet": "200 Depot Way",
            "FacCity": "Clarksburg",
            "FacState": "WV",
            "FacCounty": "Harrison",
            "FacLat": "39.28",
            "FacLong": "-80.34",
            "FacDateLastInspection": "08/14/2016",
            "FacDerivedStnFlag": "Y",
            "FacPercent3yrQuartersInNC": "50"
        }
        fac = adapter._normalize_echo_facility(raw_f)
        self.assertIsNotNone(fac)
        self.assertEqual(fac.facility_id, "ECHO-110000998877")
        self.assertEqual(fac.name, "MOUNTAIN STATE FUEL CO")
        self.assertEqual(fac.inferred_vintage_year, 2016)
        self.assertTrue(fac.violation_flag)


class TestSpatialUtilities(unittest.TestCase):
    def test_haversine_distance(self):
        # Nitro WV (38.41, -81.82) to Institute WV (38.39, -81.78) ~4.1 km
        dist = haversine_distance_km(38.41, -81.82, 38.39, -81.78)
        self.assertAlmostEqual(dist, 4.1, delta=1.0)

        # Same point -> 0.0 km
        dist_zero = haversine_distance_km(40.0, -75.0, 40.0, -75.0)
        self.assertAlmostEqual(dist_zero, 0.0, places=4)


class TestLeadExporter(unittest.TestCase):
    def test_csv_and_json_export(self):
        prospect = ProspectEvaluation(
            facility_id="EXP-01",
            name="Export Test Facility",
            owner="Export LLC",
            street="123 Test St",
            city="Charleston",
            state="WV",
            county="Kanawha",
            latitude=38.35,
            longitude=-81.63,
            tank_count=3,
            aggregate_capacity_gal=50000.0,
            max_individual_tank_gal=20000.0,
            capacity_tier="PE-Certified Plan (>10k gal Standard Industrial)",
            estimated_plan_value_min=5000.0,
            estimated_plan_value_mid=6500.0,
            estimated_plan_value_max=8000.0,
            inferred_plan_vintage=2016,
            years_since_vintage=10.0,
            is_overdue_5yr=True,
            material_modification_trigger=True,
            inspection_gap_years=5.5,
            violation_flag=True,
            distance_to_waterway_km=0.3,
            waterway_name="Kanawha River",
            overdue_score=92.5,
            priority_level="CRITICAL",
            primary_trigger_reason="Critical 5-Year Review Overdue",
            all_trigger_reasons=["5-Year Lapsed", "Intake Proximity"],
            recommended_consultant_action="Pitch PE Plan",
            monthly_lead_tier="$1,200/mo Lead Float",
            source_rail="TestRail"
        )
        rail_st = RailStatus(
            rail_name="Test Rail",
            rail_type="ARCGIS_REST",
            endpoint_url="https://example.com/arcgis/rest",
            http_status=200,
            is_live=True,
            record_count=100,
            field_schema=["id", "name"],
            latency_ms=120.0,
            last_verified_iso="2026-11-02T12:00:00Z"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "test_out.csv")
            json_path = os.path.join(tmpdir, "test_out.json")

            LeadExporter.export_csv([prospect], csv_path)
            LeadExporter.export_json([prospect], [rail_st], json_path)

            self.assertTrue(os.path.exists(csv_path))
            self.assertTrue(os.path.exists(json_path))

            # Validate CSV content
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["facility_id"], "EXP-01")
                self.assertEqual(rows[0]["priority_level"], "CRITICAL")
                self.assertEqual(rows[0]["is_overdue_5yr"], "YES")

            # Validate JSON content
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.assertIn("metadata", data)
                self.assertIn("verified_rails", data)
                self.assertIn("top_prospects", data)
                self.assertEqual(data["metadata"]["total_prospects_ranked"], 1)
                self.assertEqual(data["verified_rails"][0]["rail_name"], "Test Rail")


if __name__ == "__main__":
    unittest.main()
