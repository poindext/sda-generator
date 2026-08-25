"""
expand_facilities.py
--------------------
Patches ohio_demo.template.json in-place to expand the facility catalog
from 8 to ~46 facilities, add matching providers, extend geography to
additional Ohio counties, and add SE Ohio as a fifth region.

Run once, then regenerate the population.
"""

import json
from pathlib import Path

TEMPLATE = Path("ohio_demo.template.json")

# ---------------------------------------------------------------------------
# New facilities  (38 additions → total 46)
# ---------------------------------------------------------------------------
NEW_FACILITIES = [
    # ── NE Ohio ─────────────────────────────────────────────────────────────
    {
        "code": "UHCLEVMAIN",
        "name": "University Hospitals Cleveland Medical Center",
        "health_system_code": "UH_SYS",
        "health_system_name": "University Hospitals Health System",
        "type": "hospital",
        "city": "Cleveland",
        "county_fips": "39035",
        "address": "11100 Euclid Ave",
        "zip": "44106",
        "weight": 0.090,
        "region": "NE Ohio",
    },
    {
        "code": "METRHLT",
        "name": "MetroHealth Medical Center",
        "health_system_code": "METRO_SYS",
        "health_system_name": "MetroHealth System",
        "type": "hospital",
        "city": "Cleveland",
        "county_fips": "39035",
        "address": "2500 MetroHealth Drive",
        "zip": "44109",
        "weight": 0.065,
        "region": "NE Ohio",
    },
    {
        "code": "HILLCRHC",
        "name": "Hillcrest Hospital",
        "health_system_code": "CLEVCLINIC_SYS",
        "health_system_name": "Cleveland Clinic Health System",
        "type": "community_hospital",
        "city": "Mayfield Heights",
        "county_fips": "39035",
        "address": "6780 Mayfield Road",
        "zip": "44124",
        "weight": 0.022,
        "region": "NE Ohio",
    },
    {
        "code": "EUCLIDHS",
        "name": "Euclid Hospital",
        "health_system_code": "CLEVCLINIC_SYS",
        "health_system_name": "Cleveland Clinic Health System",
        "type": "community_hospital",
        "city": "Euclid",
        "county_fips": "39035",
        "address": "18901 Lake Shore Blvd",
        "zip": "44119",
        "weight": 0.018,
        "region": "NE Ohio",
    },
    {
        "code": "LAKEHLT",
        "name": "Lake Health Tripoint Medical Center",
        "health_system_code": "UH_SYS",
        "health_system_name": "University Hospitals Health System",
        "type": "community_hospital",
        "city": "Concord Township",
        "county_fips": "39085",
        "address": "7590 Auburn Road",
        "zip": "44077",
        "weight": 0.028,
        "region": "NE Ohio",
    },
    {
        "code": "UHELYRIA",
        "name": "UH Elyria Medical Center",
        "health_system_code": "UH_SYS",
        "health_system_name": "University Hospitals Health System",
        "type": "community_hospital",
        "city": "Elyria",
        "county_fips": "39093",
        "address": "630 East River Street",
        "zip": "44035",
        "weight": 0.025,
        "region": "NE Ohio",
    },
    {
        "code": "STRNBRD",
        "name": "St. Elizabeth Boardman Hospital",
        "health_system_code": "MERCY_SYS",
        "health_system_name": "Mercy Health (Bon Secours Mercy Health)",
        "type": "community_hospital",
        "city": "Boardman",
        "county_fips": "39099",
        "address": "8401 Market Street",
        "zip": "44512",
        "weight": 0.022,
        "region": "NE Ohio",
    },
    {
        "code": "TRUMBHC",
        "name": "Trumbull Regional Medical Center",
        "health_system_code": "STEWARD_SYS",
        "health_system_name": "Steward Health Care",
        "type": "community_hospital",
        "city": "Warren",
        "county_fips": "39155",
        "address": "1350 East Market Street",
        "zip": "44483",
        "weight": 0.018,
        "region": "NE Ohio",
    },
    {
        "code": "AULTMHC",
        "name": "Aultman Hospital",
        "health_system_code": "AULTMAN_SYS",
        "health_system_name": "Aultman Health Foundation",
        "type": "community_hospital",
        "city": "Canton",
        "county_fips": "39151",
        "address": "2600 Sixth Street SW",
        "zip": "44710",
        "weight": 0.025,
        "region": "NE Ohio",
    },
    {
        "code": "CLEFQHC",
        "name": "Care Alliance Health Center",
        "health_system_code": "CAREALL_SYS",
        "health_system_name": "Care Alliance Health Center (FQHC)",
        "type": "fqhc",
        "city": "Cleveland",
        "county_fips": "39035",
        "address": "2030 Ontario Street",
        "zip": "44115",
        "weight": 0.012,
        "region": "NE Ohio",
    },
    {
        "code": "NEOFAM1",
        "name": "Family Health Services of Lorain County",
        "health_system_code": "NEOFAM_SYS",
        "health_system_name": "Family Health Services",
        "type": "family_practice",
        "city": "Lorain",
        "county_fips": "39093",
        "address": "1001 Broadway Ave",
        "zip": "44052",
        "weight": 0.010,
        "region": "NE Ohio",
    },
    # ── Central Ohio ────────────────────────────────────────────────────────
    {
        "code": "GRANTOH",
        "name": "OhioHealth Grant Medical Center",
        "health_system_code": "OHIOHLTH_SYS",
        "health_system_name": "OhioHealth",
        "type": "hospital",
        "city": "Columbus",
        "county_fips": "39049",
        "address": "111 South Grant Avenue",
        "zip": "43215",
        "weight": 0.070,
        "region": "Central Ohio",
    },
    {
        "code": "RIVSIDE",
        "name": "OhioHealth Riverside Methodist Hospital",
        "health_system_code": "OHIOHLTH_SYS",
        "health_system_name": "OhioHealth",
        "type": "hospital",
        "city": "Columbus",
        "county_fips": "39049",
        "address": "3535 Olentangy River Road",
        "zip": "43214",
        "weight": 0.065,
        "region": "Central Ohio",
    },
    {
        "code": "MTCARML",
        "name": "Mount Carmel St. Ann's",
        "health_system_code": "MTCARML_SYS",
        "health_system_name": "Mount Carmel Health System",
        "type": "hospital",
        "city": "Columbus",
        "county_fips": "39049",
        "address": "500 South Cleveland Avenue",
        "zip": "43081",
        "weight": 0.055,
        "region": "Central Ohio",
    },
    {
        "code": "NATNWID",
        "name": "Nationwide Children's Hospital",
        "health_system_code": "NATNWID_SYS",
        "health_system_name": "Nationwide Children's Hospital",
        "type": "hospital",
        "city": "Columbus",
        "county_fips": "39049",
        "address": "700 Children's Drive",
        "zip": "43205",
        "weight": 0.040,
        "max_patient_age": 21,
        "region": "Central Ohio",
    },
    {
        "code": "OHIODOC",
        "name": "OhioHealth Doctors Hospital",
        "health_system_code": "OHIOHLTH_SYS",
        "health_system_name": "OhioHealth",
        "type": "community_hospital",
        "city": "Columbus",
        "county_fips": "39049",
        "address": "5100 West Broad Street",
        "zip": "43228",
        "weight": 0.028,
        "region": "Central Ohio",
    },
    {
        "code": "COLFQHC",
        "name": "PrimaryOne Health",
        "health_system_code": "PRIMONE_SYS",
        "health_system_name": "PrimaryOne Health (FQHC)",
        "type": "fqhc",
        "city": "Columbus",
        "county_fips": "39049",
        "address": "1390 East Broad Street",
        "zip": "43205",
        "weight": 0.014,
        "region": "Central Ohio",
    },
    {
        "code": "LANCHOS",
        "name": "Fairfield Medical Center",
        "health_system_code": "FAIRMDC_SYS",
        "health_system_name": "Fairfield Medical Center",
        "type": "community_hospital",
        "city": "Lancaster",
        "county_fips": "39045",
        "address": "401 North Ewing Street",
        "zip": "43130",
        "weight": 0.020,
        "region": "Central Ohio",
    },
    {
        "code": "NEWARKLH",
        "name": "Licking Memorial Hospital",
        "health_system_code": "LICKMEM_SYS",
        "health_system_name": "Licking Memorial Health Systems",
        "type": "community_hospital",
        "city": "Newark",
        "county_fips": "39089",
        "address": "1320 West Main Street",
        "zip": "43055",
        "weight": 0.018,
        "region": "Central Ohio",
    },
    # ── SW Ohio ──────────────────────────────────────────────────────────────
    {
        "code": "TRIHLTH",
        "name": "TriHealth Good Samaritan Hospital",
        "health_system_code": "TRIHLTH_SYS",
        "health_system_name": "TriHealth",
        "type": "hospital",
        "city": "Cincinnati",
        "county_fips": "39061",
        "address": "375 Dixmyth Avenue",
        "zip": "45220",
        "weight": 0.055,
        "region": "SW Ohio",
    },
    {
        "code": "BETHNRTH",
        "name": "Bethesda North Hospital",
        "health_system_code": "TRIHLTH_SYS",
        "health_system_name": "TriHealth",
        "type": "hospital",
        "city": "Montgomery",
        "county_fips": "39061",
        "address": "10500 Montgomery Road",
        "zip": "45242",
        "weight": 0.045,
        "region": "SW Ohio",
    },
    {
        "code": "KTHNET",
        "name": "Kettering Health Main Campus",
        "health_system_code": "KTHNET_SYS",
        "health_system_name": "Kettering Health",
        "type": "hospital",
        "city": "Kettering",
        "county_fips": "39113",
        "address": "3535 Southern Blvd",
        "zip": "45429",
        "weight": 0.050,
        "region": "SW Ohio",
    },
    {
        "code": "MIAVLHS",
        "name": "Miami Valley Hospital",
        "health_system_code": "PREMIER_SYS",
        "health_system_name": "Premier Health",
        "type": "hospital",
        "city": "Dayton",
        "county_fips": "39113",
        "address": "1 Wyoming Street",
        "zip": "45409",
        "weight": 0.045,
        "region": "SW Ohio",
    },
    {
        "code": "SPFLDMC",
        "name": "Springfield Regional Medical Center",
        "health_system_code": "MERCY_SYS",
        "health_system_name": "Mercy Health (Bon Secours Mercy Health)",
        "type": "community_hospital",
        "city": "Springfield",
        "county_fips": "39023",
        "address": "100 Medical Center Drive",
        "zip": "45504",
        "weight": 0.022,
        "region": "SW Ohio",
    },
    {
        "code": "HAMILTOH",
        "name": "Fort Hamilton Hospital",
        "health_system_code": "PREMIER_SYS",
        "health_system_name": "Premier Health",
        "type": "community_hospital",
        "city": "Hamilton",
        "county_fips": "39017",
        "address": "630 Eaton Avenue",
        "zip": "45013",
        "weight": 0.020,
        "region": "SW Ohio",
    },
    {
        "code": "CINCIFD",
        "name": "Cincinnati Health Department - Clement Health Center",
        "health_system_code": "CINCIHD_SYS",
        "health_system_name": "Cincinnati Health Department (FQHC)",
        "type": "fqhc",
        "city": "Cincinnati",
        "county_fips": "39061",
        "address": "3101 Burnet Avenue",
        "zip": "45229",
        "weight": 0.012,
        "region": "SW Ohio",
    },
    {
        "code": "SWOFAM1",
        "name": "Dayton Family Health Center",
        "health_system_code": "DAYTONFC_SYS",
        "health_system_name": "Dayton Family Health Center",
        "type": "family_practice",
        "city": "Dayton",
        "county_fips": "39113",
        "address": "2611 Wayne Avenue",
        "zip": "45420",
        "weight": 0.010,
        "region": "SW Ohio",
    },
    # ── NW Ohio ──────────────────────────────────────────────────────────────
    {
        "code": "PROMSTL",
        "name": "ProMedica St. Luke's Hospital",
        "health_system_code": "PROMEDICA_SYS",
        "health_system_name": "ProMedica Health System",
        "type": "community_hospital",
        "city": "Maumee",
        "county_fips": "39173",
        "address": "5901 Monclova Road",
        "zip": "43537",
        "weight": 0.028,
        "region": "NW Ohio",
    },
    {
        "code": "BLANCVL",
        "name": "Blanchard Valley Regional Health Center",
        "health_system_code": "BVHS_SYS",
        "health_system_name": "Blanchard Valley Health System",
        "type": "community_hospital",
        "city": "Findlay",
        "county_fips": "39063",
        "address": "145 West Wallace Street",
        "zip": "45840",
        "weight": 0.022,
        "region": "NW Ohio",
    },
    {
        "code": "LIMALML",
        "name": "Lima Memorial Health System",
        "health_system_code": "LIMAMEM_SYS",
        "health_system_name": "Lima Memorial Health System",
        "type": "community_hospital",
        "city": "Lima",
        "county_fips": "39003",
        "address": "1001 Bellefontaine Avenue",
        "zip": "45804",
        "weight": 0.020,
        "region": "NW Ohio",
    },
    {
        "code": "MERCLIMA",
        "name": "Mercy Health St. Rita's Medical Center",
        "health_system_code": "MERCY_SYS",
        "health_system_name": "Mercy Health (Bon Secours Mercy Health)",
        "type": "community_hospital",
        "city": "Lima",
        "county_fips": "39003",
        "address": "730 West Market Street",
        "zip": "45801",
        "weight": 0.018,
        "region": "NW Ohio",
    },
    {
        "code": "NWOFQHC",
        "name": "CHP - The Center for Health and Prevention",
        "health_system_code": "CHPNWO_SYS",
        "health_system_name": "CHP Northwest Ohio (FQHC)",
        "type": "fqhc",
        "city": "Toledo",
        "county_fips": "39095",
        "address": "600 Jefferson Avenue",
        "zip": "43604",
        "weight": 0.010,
        "region": "NW Ohio",
    },
    # ── SE Ohio (new region) ─────────────────────────────────────────────────
    {
        "code": "ADENAHC",
        "name": "Adena Regional Medical Center",
        "health_system_code": "ADENA_SYS",
        "health_system_name": "Adena Health System",
        "type": "hospital",
        "city": "Chillicothe",
        "county_fips": "39141",
        "address": "272 Hospital Road",
        "zip": "45601",
        "weight": 0.025,
        "region": "SE Ohio",
    },
    {
        "code": "ZANESHC",
        "name": "Genesis Healthcare System",
        "health_system_code": "GENESIS_SYS",
        "health_system_name": "Genesis Healthcare System",
        "type": "community_hospital",
        "city": "Zanesville",
        "county_fips": "39119",
        "address": "2951 Maple Avenue",
        "zip": "43701",
        "weight": 0.020,
        "region": "SE Ohio",
    },
    {
        "code": "OBLENHC",
        "name": "OhioHealth O'Bleness Hospital",
        "health_system_code": "OHIOHLTH_SYS",
        "health_system_name": "OhioHealth",
        "type": "community_hospital",
        "city": "Athens",
        "county_fips": "39009",
        "address": "55 Hospital Drive",
        "zip": "45701",
        "weight": 0.018,
        "region": "SE Ohio",
    },
    {
        "code": "HOLZERH",
        "name": "Holzer Medical Center",
        "health_system_code": "HOLZER_SYS",
        "health_system_name": "Holzer Health System",
        "type": "community_hospital",
        "city": "Gallipolis",
        "county_fips": "39053",
        "address": "100 Jackson Pike",
        "zip": "45631",
        "weight": 0.015,
        "region": "SE Ohio",
    },
    {
        "code": "LAWRNCH",
        "name": "SOMC - Southern Ohio Medical Center",
        "health_system_code": "SOMC_SYS",
        "health_system_name": "Southern Ohio Medical Center",
        "type": "community_hospital",
        "city": "Portsmouth",
        "county_fips": "39145",
        "address": "1805 27th Street",
        "zip": "45662",
        "weight": 0.012,
        "region": "SE Ohio",
    },
    {
        "code": "SEOHFQHC",
        "name": "Muskingum Valley Health Centers",
        "health_system_code": "MVHC_SYS",
        "health_system_name": "Muskingum Valley Health Centers (FQHC)",
        "type": "fqhc",
        "city": "Coshocton",
        "county_fips": "39031",
        "address": "825 Walnut Street",
        "zip": "43812",
        "weight": 0.010,
        "region": "SE Ohio",
    },
]

# ---------------------------------------------------------------------------
# New providers  (2–3 per new facility, DR023 onward)
# ---------------------------------------------------------------------------
NEW_PROVIDERS = [
    # UH Cleveland Main
    {"code": "DR023", "name": "Dr. Patricia Nguyen",     "specialty": "Internal Medicine",     "facility_code": "UHCLEVMAIN"},
    {"code": "DR024", "name": "Dr. James Kowalczyk",     "specialty": "Cardiology",             "facility_code": "UHCLEVMAIN"},
    {"code": "DR025", "name": "Dr. Fatima Al-Hassan",    "specialty": "Endocrinology",          "facility_code": "UHCLEVMAIN"},
    # MetroHealth
    {"code": "DR026", "name": "Dr. Carlos Rivera",       "specialty": "Family Medicine",        "facility_code": "METRHLT"},
    {"code": "DR027", "name": "Dr. Kesha Williams",      "specialty": "Internal Medicine",      "facility_code": "METRHLT"},
    {"code": "DR028", "name": "Dr. Igor Petrov",         "specialty": "Nephrology",             "facility_code": "METRHLT"},
    # Hillcrest
    {"code": "DR029", "name": "Dr. Linda Park",          "specialty": "Internal Medicine",      "facility_code": "HILLCRHC"},
    {"code": "DR030", "name": "Dr. Michael Brennan",     "specialty": "Family Medicine",        "facility_code": "HILLCRHC"},
    # Euclid Hospital
    {"code": "DR031", "name": "Dr. Yolanda Torres",      "specialty": "Internal Medicine",      "facility_code": "EUCLIDHS"},
    {"code": "DR032", "name": "Dr. Kenneth Marsh",       "specialty": "Family Medicine",        "facility_code": "EUCLIDHS"},
    # Lake Health Tripoint
    {"code": "DR033", "name": "Dr. Rebecca Simmons",     "specialty": "Internal Medicine",      "facility_code": "LAKEHLT"},
    {"code": "DR034", "name": "Dr. Omar Khalil",         "specialty": "Family Medicine",        "facility_code": "LAKEHLT"},
    # UH Elyria
    {"code": "DR035", "name": "Dr. Svetlana Volkov",     "specialty": "Internal Medicine",      "facility_code": "UHELYRIA"},
    {"code": "DR036", "name": "Dr. Dennis Achebe",       "specialty": "Family Medicine",        "facility_code": "UHELYRIA"},
    # St. Elizabeth Boardman
    {"code": "DR037", "name": "Dr. Nancy Fitzgerald",    "specialty": "Internal Medicine",      "facility_code": "STRNBRD"},
    {"code": "DR038", "name": "Dr. Rashid Okonkwo",      "specialty": "Family Medicine",        "facility_code": "STRNBRD"},
    # Trumbull Regional
    {"code": "DR039", "name": "Dr. Helen Ziegler",       "specialty": "Internal Medicine",      "facility_code": "TRUMBHC"},
    {"code": "DR040", "name": "Dr. Marcus Webb",         "specialty": "Family Medicine",        "facility_code": "TRUMBHC"},
    # Aultman Hospital
    {"code": "DR041", "name": "Dr. Joan Carrington",     "specialty": "Internal Medicine",      "facility_code": "AULTMHC"},
    {"code": "DR042", "name": "Dr. Phillip Adeyemi",     "specialty": "Family Medicine",        "facility_code": "AULTMHC"},
    # Care Alliance FQHC
    {"code": "DR043", "name": "Dr. Maria Santos",        "specialty": "Family Medicine",        "facility_code": "CLEFQHC"},
    {"code": "DR044", "name": "Dr. Jerome Butler",       "specialty": "Internal Medicine",      "facility_code": "CLEFQHC"},
    # NE Ohio Family Practice
    {"code": "DR045", "name": "Dr. Sarah Kowalski",      "specialty": "Family Medicine",        "facility_code": "NEOFAM1"},
    # OhioHealth Grant
    {"code": "DR046", "name": "Dr. Andrew Sullivan",     "specialty": "Cardiology",             "facility_code": "GRANTOH"},
    {"code": "DR047", "name": "Dr. Tina Nakamura",       "specialty": "Internal Medicine",      "facility_code": "GRANTOH"},
    {"code": "DR048", "name": "Dr. Victor Oduya",        "specialty": "Endocrinology",          "facility_code": "GRANTOH"},
    # Riverside Methodist
    {"code": "DR049", "name": "Dr. Christine Delacroix", "specialty": "Internal Medicine",      "facility_code": "RIVSIDE"},
    {"code": "DR050", "name": "Dr. Samuel Okafor",       "specialty": "Family Medicine",        "facility_code": "RIVSIDE"},
    {"code": "DR051", "name": "Dr. Beth Hartmann",       "specialty": "Cardiology",             "facility_code": "RIVSIDE"},
    # Mount Carmel
    {"code": "DR052", "name": "Dr. Ricardo Espinoza",    "specialty": "Internal Medicine",      "facility_code": "MTCARML"},
    {"code": "DR053", "name": "Dr. Donna Claypool",      "specialty": "Family Medicine",        "facility_code": "MTCARML"},
    {"code": "DR054", "name": "Dr. Babatunde Adewale",   "specialty": "Hospitalist",            "facility_code": "MTCARML"},
    # Nationwide Children's
    {"code": "DR055", "name": "Dr. Nicole Huang",        "specialty": "Pediatric Endocrinology","facility_code": "NATNWID"},
    {"code": "DR056", "name": "Dr. William Rios",        "specialty": "Pediatrics",             "facility_code": "NATNWID"},
    # OhioHealth Doctors
    {"code": "DR057", "name": "Dr. Angela Freeman",      "specialty": "Internal Medicine",      "facility_code": "OHIODOC"},
    {"code": "DR058", "name": "Dr. Patrick Nyamweya",    "specialty": "Family Medicine",        "facility_code": "OHIODOC"},
    # PrimaryOne FQHC
    {"code": "DR059", "name": "Dr. Rosa Gutierrez",      "specialty": "Family Medicine",        "facility_code": "COLFQHC"},
    {"code": "DR060", "name": "Dr. David Anand",         "specialty": "Internal Medicine",      "facility_code": "COLFQHC"},
    # Fairfield Medical
    {"code": "DR061", "name": "Dr. Constance MacPherson","specialty": "Family Medicine",        "facility_code": "LANCHOS"},
    {"code": "DR062", "name": "Dr. Emeka Eze",           "specialty": "Internal Medicine",      "facility_code": "LANCHOS"},
    # Licking Memorial
    {"code": "DR063", "name": "Dr. Sandra Beaumont",     "specialty": "Family Medicine",        "facility_code": "NEWARKLH"},
    {"code": "DR064", "name": "Dr. Frank Anyanwu",       "specialty": "Internal Medicine",      "facility_code": "NEWARKLH"},
    # TriHealth Good Samaritan
    {"code": "DR065", "name": "Dr. Laura Whitfield",     "specialty": "Cardiology",             "facility_code": "TRIHLTH"},
    {"code": "DR066", "name": "Dr. Kwame Asante",        "specialty": "Internal Medicine",      "facility_code": "TRIHLTH"},
    {"code": "DR067", "name": "Dr. Diana Bergmann",      "specialty": "Endocrinology",          "facility_code": "TRIHLTH"},
    # Bethesda North
    {"code": "DR068", "name": "Dr. Steven Chukwu",       "specialty": "Internal Medicine",      "facility_code": "BETHNRTH"},
    {"code": "DR069", "name": "Dr. Jennifer Malone",     "specialty": "Family Medicine",        "facility_code": "BETHNRTH"},
    {"code": "DR070", "name": "Dr. Raj Krishnamurthy",   "specialty": "Hospitalist",            "facility_code": "BETHNRTH"},
    # Kettering Health
    {"code": "DR071", "name": "Dr. Olivia Schreiber",    "specialty": "Internal Medicine",      "facility_code": "KTHNET"},
    {"code": "DR072", "name": "Dr. Maurice Benson",      "specialty": "Family Medicine",        "facility_code": "KTHNET"},
    {"code": "DR073", "name": "Dr. Amy Watanabe",        "specialty": "Cardiology",             "facility_code": "KTHNET"},
    # Miami Valley Hospital
    {"code": "DR074", "name": "Dr. Charles Mbeki",       "specialty": "Internal Medicine",      "facility_code": "MIAVLHS"},
    {"code": "DR075", "name": "Dr. Allison Drake",       "specialty": "Family Medicine",        "facility_code": "MIAVLHS"},
    {"code": "DR076", "name": "Dr. Tobias Hoffmann",     "specialty": "Hospitalist",            "facility_code": "MIAVLHS"},
    # Springfield Regional
    {"code": "DR077", "name": "Dr. Pamela Owens",        "specialty": "Family Medicine",        "facility_code": "SPFLDMC"},
    {"code": "DR078", "name": "Dr. Hassan Musa",         "specialty": "Internal Medicine",      "facility_code": "SPFLDMC"},
    # Fort Hamilton
    {"code": "DR079", "name": "Dr. Beverly Crandall",    "specialty": "Family Medicine",        "facility_code": "HAMILTOH"},
    {"code": "DR080", "name": "Dr. Nnamdi Obi",          "specialty": "Internal Medicine",      "facility_code": "HAMILTOH"},
    # Cincinnati Health Dept FQHC
    {"code": "DR081", "name": "Dr. Veronica Salinas",    "specialty": "Family Medicine",        "facility_code": "CINCIFD"},
    {"code": "DR082", "name": "Dr. Terrence Boateng",    "specialty": "Internal Medicine",      "facility_code": "CINCIFD"},
    # Dayton Family Practice
    {"code": "DR083", "name": "Dr. Wendy Carmichael",    "specialty": "Family Medicine",        "facility_code": "SWOFAM1"},
    # ProMedica St. Luke's
    {"code": "DR084", "name": "Dr. Gregory Papadopoulos","specialty": "Internal Medicine",      "facility_code": "PROMSTL"},
    {"code": "DR085", "name": "Dr. Suzanne Mbatha",      "specialty": "Family Medicine",        "facility_code": "PROMSTL"},
    # Blanchard Valley
    {"code": "DR086", "name": "Dr. Harold Greenfield",   "specialty": "Family Medicine",        "facility_code": "BLANCVL"},
    {"code": "DR087", "name": "Dr. Ifunaya Nwosu",       "specialty": "Internal Medicine",      "facility_code": "BLANCVL"},
    # Lima Memorial
    {"code": "DR088", "name": "Dr. Kathleen Strickland",  "specialty": "Family Medicine",       "facility_code": "LIMALML"},
    {"code": "DR089", "name": "Dr. Emmanuel Abara",       "specialty": "Internal Medicine",     "facility_code": "LIMALML"},
    # Mercy St. Rita's Lima
    {"code": "DR090", "name": "Dr. Florence Nzeka",       "specialty": "Internal Medicine",     "facility_code": "MERCLIMA"},
    {"code": "DR091", "name": "Dr. Brian Kowalczyk",      "specialty": "Family Medicine",       "facility_code": "MERCLIMA"},
    # CHP NW Ohio FQHC
    {"code": "DR092", "name": "Dr. Amara Diallo",         "specialty": "Family Medicine",       "facility_code": "NWOFQHC"},
    # Adena Regional (SE Ohio)
    {"code": "DR093", "name": "Dr. Calvin Hutchins",      "specialty": "Internal Medicine",     "facility_code": "ADENAHC"},
    {"code": "DR094", "name": "Dr. Miriam Osei",          "specialty": "Family Medicine",       "facility_code": "ADENAHC"},
    {"code": "DR095", "name": "Dr. Leonard Pryor",        "specialty": "Hospitalist",           "facility_code": "ADENAHC"},
    # Genesis Healthcare
    {"code": "DR096", "name": "Dr. Agnes Kamara",         "specialty": "Internal Medicine",     "facility_code": "ZANESHC"},
    {"code": "DR097", "name": "Dr. Timothy Brewster",     "specialty": "Family Medicine",       "facility_code": "ZANESHC"},
    # OhioHealth O'Bleness
    {"code": "DR098", "name": "Dr. Leila Mousavi",        "specialty": "Family Medicine",       "facility_code": "OBLENHC"},
    {"code": "DR099", "name": "Dr. George Abubakar",      "specialty": "Internal Medicine",     "facility_code": "OBLENHC"},
    # Holzer Medical Center
    {"code": "DR100", "name": "Dr. Ruth Eberhart",        "specialty": "Internal Medicine",     "facility_code": "HOLZERH"},
    {"code": "DR101", "name": "Dr. Samuel Dieterle",      "specialty": "Family Medicine",       "facility_code": "HOLZERH"},
    # SOMC
    {"code": "DR102", "name": "Dr. Christine Nwokem",     "specialty": "Family Medicine",       "facility_code": "LAWRNCH"},
    {"code": "DR103", "name": "Dr. Arthur Schaefer",      "specialty": "Internal Medicine",     "facility_code": "LAWRNCH"},
    # Muskingum Valley FQHC
    {"code": "DR104", "name": "Dr. Gloria Mensah",        "specialty": "Family Medicine",       "facility_code": "SEOHFQHC"},
]

# ---------------------------------------------------------------------------
# New geography locations  (counties not yet in template)
# ---------------------------------------------------------------------------
NEW_GEO_LOCATIONS = [
    # NE Ohio — additional counties
    {"county": "Lake",      "county_fips": "39085", "state_code": "OH", "weight": 0.04,
     "rurality": "suburban", "region": "NE Ohio",
     "cities": [{"name": "Painesville", "zips": ["44077"]},
                {"name": "Mentor", "zips": ["44060", "44061"]}]},
    {"county": "Trumbull",  "county_fips": "39155", "state_code": "OH", "weight": 0.03,
     "rurality": "suburban", "region": "NE Ohio",
     "cities": [{"name": "Warren", "zips": ["44483", "44484"]},
                {"name": "Niles", "zips": ["44446"]}]},
    # Central Ohio — additional counties
    {"county": "Licking",   "county_fips": "39089", "state_code": "OH", "weight": 0.025,
     "rurality": "suburban", "region": "Central Ohio",
     "cities": [{"name": "Newark", "zips": ["43055", "43056"]},
                {"name": "Heath", "zips": ["43056"]}]},
    {"county": "Fairfield", "county_fips": "39045", "state_code": "OH", "weight": 0.020,
     "rurality": "suburban", "region": "Central Ohio",
     "cities": [{"name": "Lancaster", "zips": ["43130", "43140"]},
                {"name": "Pickerington", "zips": ["43147"]}]},
    # SW Ohio — additional counties
    {"county": "Clark",     "county_fips": "39023", "state_code": "OH", "weight": 0.025,
     "rurality": "suburban", "region": "SW Ohio",
     "cities": [{"name": "Springfield", "zips": ["45502", "45503", "45504"]}]},
    {"county": "Warren",    "county_fips": "39165", "state_code": "OH", "weight": 0.028,
     "rurality": "suburban", "region": "SW Ohio",
     "cities": [{"name": "Mason", "zips": ["45040"]},
                {"name": "Lebanon", "zips": ["45036"]}]},
    {"county": "Greene",    "county_fips": "39057", "state_code": "OH", "weight": 0.022,
     "rurality": "suburban", "region": "SW Ohio",
     "cities": [{"name": "Beavercreek", "zips": ["45430", "45431"]},
                {"name": "Xenia", "zips": ["45385"]}]},
    # NW Ohio — additional counties
    {"county": "Wood",      "county_fips": "39173", "state_code": "OH", "weight": 0.025,
     "rurality": "suburban", "region": "NW Ohio",
     "cities": [{"name": "Bowling Green", "zips": ["43402"]},
                {"name": "Perrysburg", "zips": ["43551"]}]},
    {"county": "Hancock",   "county_fips": "39063", "state_code": "OH", "weight": 0.020,
     "rurality": "suburban", "region": "NW Ohio",
     "cities": [{"name": "Findlay", "zips": ["45840", "45839"]}]},
    {"county": "Allen",     "county_fips": "39003", "state_code": "OH", "weight": 0.020,
     "rurality": "suburban", "region": "NW Ohio",
     "cities": [{"name": "Lima", "zips": ["45801", "45804", "45805"]}]},
    # SE Ohio — new region
    {"county": "Ross",      "county_fips": "39141", "state_code": "OH", "weight": 0.018,
     "rurality": "rural", "region": "SE Ohio",
     "cities": [{"name": "Chillicothe", "zips": ["45601", "45601"]}]},
    {"county": "Muskingum", "county_fips": "39119", "state_code": "OH", "weight": 0.016,
     "rurality": "rural", "region": "SE Ohio",
     "cities": [{"name": "Zanesville", "zips": ["43701", "43702"]}]},
    {"county": "Athens",    "county_fips": "39009", "state_code": "OH", "weight": 0.012,
     "rurality": "rural", "region": "SE Ohio",
     "cities": [{"name": "Athens", "zips": ["45701"]},
                {"name": "The Plains", "zips": ["45780"]}]},
    {"county": "Gallia",    "county_fips": "39053", "state_code": "OH", "weight": 0.010,
     "rurality": "rural", "region": "SE Ohio",
     "cities": [{"name": "Gallipolis", "zips": ["45631"]}]},
    {"county": "Scioto",    "county_fips": "39145", "state_code": "OH", "weight": 0.012,
     "rurality": "rural", "region": "SE Ohio",
     "cities": [{"name": "Portsmouth", "zips": ["45662"]}]},
    {"county": "Coshocton", "county_fips": "39031", "state_code": "OH", "weight": 0.010,
     "rurality": "rural", "region": "SE Ohio",
     "cities": [{"name": "Coshocton", "zips": ["43812"]}]},
]


def main():
    with open(TEMPLATE) as f:
        tmpl = json.load(f)

    # -- Facilities ----------------------------------------------------------
    existing_codes = {f["code"] for f in tmpl["facilities"]}
    added_facs = [f for f in NEW_FACILITIES if f["code"] not in existing_codes]
    tmpl["facilities"].extend(added_facs)
    print(f"Facilities: {len(existing_codes)} → {len(tmpl['facilities'])}"
          f"  (+{len(added_facs)})")

    # -- Providers -----------------------------------------------------------
    existing_prov_codes = {p["code"] for p in tmpl["providers"]}
    added_provs = [p for p in NEW_PROVIDERS if p["code"] not in existing_prov_codes]
    tmpl["providers"].extend(added_provs)
    print(f"Providers:  {len(existing_prov_codes)} → {len(tmpl['providers'])}"
          f"  (+{len(added_provs)})")

    # -- Geography locations -------------------------------------------------
    existing_fips = {g["county_fips"] for g in tmpl["geography"]["locations"]}
    added_geos = [g for g in NEW_GEO_LOCATIONS if g["county_fips"] not in existing_fips]
    tmpl["geography"]["locations"].extend(added_geos)
    print(f"Geo locations: {len(existing_fips)} → {len(tmpl['geography']['locations'])}"
          f"  (+{len(added_geos)})")

    # -- Adjacent regions (add SE Ohio) --------------------------------------
    adj = tmpl["multi_facility"]["adjacent_regions"]
    adj["SE Ohio"] = ["Central Ohio", "SW Ohio"]
    # Central Ohio now also borders SE Ohio
    if "Central Ohio" in adj and "SE Ohio" not in adj["Central Ohio"]:
        adj["Central Ohio"].append("SE Ohio")
    # SW Ohio now also borders SE Ohio
    if "SW Ohio" in adj and "SE Ohio" not in adj["SW Ohio"]:
        adj["SW Ohio"].append("SE Ohio")
    print(f"Adjacent regions updated: SE Ohio added")

    # -- Verify facility codes referenced by providers exist -----------------
    fac_codes = {f["code"] for f in tmpl["facilities"]}
    orphan_provs = [p for p in tmpl["providers"] if p["facility_code"] not in fac_codes]
    if orphan_provs:
        print(f"WARNING: {len(orphan_provs)} providers reference unknown facility codes:")
        for p in orphan_provs:
            print(f"  {p['code']} → {p['facility_code']}")

    # -- Verify geo county_fips referenced by facilities exist ---------------
    geo_fips = {g["county_fips"] for g in tmpl["geography"]["locations"]}
    missing_fips = {f["county_fips"] for f in tmpl["facilities"]
                    if f.get("county_fips") and f["county_fips"] not in geo_fips}
    if missing_fips:
        print(f"WARNING: facilities reference county_fips not in geography: {missing_fips}")

    with open(TEMPLATE, "w") as f:
        json.dump(tmpl, f, indent=2)
    print(f"\nTemplate written: {TEMPLATE}")
    print(f"  Total facilities: {len(tmpl['facilities'])}")
    print(f"  Total providers:  {len(tmpl['providers'])}")
    print(f"  Total geo locations: {len(tmpl['geography']['locations'])}")


if __name__ == "__main__":
    main()
