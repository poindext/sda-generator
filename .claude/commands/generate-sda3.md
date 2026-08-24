Generate realistic InterSystems HealthShare SDA3 XML sample data for the following scenario:

$ARGUMENTS

---

## Rules

### Container structure — CRITICAL

`<Patient>` contains **only demographics**. All clinical sections (`<Encounters>`, `<Diagnoses>`, `<Medications>`, `<Allergies>`, `<LabOrders>`, `<Observations>`, `<Problems>`, `<Procedures>`, `<Documents>`, `<Vaccinations>`) are **direct children of `<Container>`**, placed after `</Patient>`. They must **never** be nested inside `<Patient>`.

The XSD enforces strict section ordering inside `<Container>`. **Never reorder, never emit empty sections.**

```xml
<Container>
  <Patient>...</Patient>            <!-- demographics only -->
  <Encounters>...</Encounters>
  <Allergies>...</Allergies>              <!-- BEFORE IllnessHistories and Diagnoses -->
  <IllnessHistories>...</IllnessHistories> <!-- Medical History tab; BEFORE SocialHistories -->
  <SocialHistories>...</SocialHistories>  <!-- BEFORE Diagnoses -->
  <FamilyHistories>...</FamilyHistories>  <!-- BEFORE Diagnoses -->
  <Diagnoses>...</Diagnoses>
  <Observations>...</Observations>  <!-- BEFORE Medications -->
  <Problems>...</Problems>
  <Procedures>...</Procedures>
  <Documents>...</Documents>        <!-- AFTER Procedures, BEFORE LabOrders -->
  <LabOrders>...</LabOrders>
  <RadOrders>...</RadOrders>        <!-- AFTER LabOrders, BEFORE Medications -->
  <Medications>...</Medications>    <!-- AFTER Observations, Procedures, LabOrders, RadOrders -->
  <Vaccinations>...</Vaccinations>
  <!-- ... many optional sections skipped ... -->
  <MedicalClaims>...</MedicalClaims>    <!-- AFTER Vaccinations, BEFORE SocialDeterminants -->
  <SocialDeterminants>...</SocialDeterminants>  <!-- AFTER MedicalClaims, near end -->
</Container>
```

**Common ordering mistakes that cause validation failure:**
- Putting `<IllnessHistories>` after `<SocialHistories>` or `<Diagnoses>` — invalid
- Putting `<SocialHistories>` or `<FamilyHistories>` after `<Diagnoses>` — invalid
- Putting `<Medications>` before `<Observations>` — invalid
- Putting `<Allergies>` after `<Diagnoses>` — invalid
- Putting `<Documents>` after `<LabOrders>` — invalid
- Putting `<MedicalClaims>` before `<Vaccinations>` or after `<SocialDeterminants>` — invalid
- Putting `<SocialDeterminants>` before `<MedicalClaims>` — invalid
- Emitting empty sections like `<Diagnoses></Diagnoses>` — omit sections with no records entirely

### What to produce
- Valid SDA3 XML with a root `<Container>` element (no XML namespace declarations needed)
- Every `<Container>` **must contain exactly one `<Patient>`** — this is always required
- Include only the other clinical sections relevant to the scenario — do not include every section just because it exists
- For each clinical section, include 1–3 records unless the scenario specifies a quantity
- Populate **required and commonly-used fields only** — leave optional fields absent unless they add clinical realism to the scenario
- All data must be clinically coherent: diagnoses, medications, labs, and allergies should relate to each other and to the stated scenario
- **Always wrap entries in their plural container tag even when there is only one entry.** A single encounter must still be: `<Encounters><Encounter>...</Encounter></Encounters>`. This applies to every section without exception: `<Encounters>`, `<Allergies>`, `<IllnessHistories>`, `<SocialHistories>`, `<FamilyHistories>`, `<Diagnoses>`, `<Observations>`, `<Problems>`, `<Procedures>`, `<Documents>`, `<LabOrders>`, `<Medications>`, `<Vaccinations>`, `<SocialDeterminants>`, `<PatientNumbers>`, `<ResultItems>`. Never emit a bare child element without its plural wrapper.

### Date format — ISO 8601 required
Never use HL7 YYYYMMDD format. Never use date-only `YYYY-MM-DD`.

ALL `TimeStamp` fields require the full ISO 8601 datetime: `YYYY-MM-DDTHH:MM:SSZ`
- When a precise time is not meaningful, use `T00:00:00Z` — e.g. `2024-03-15T00:00:00Z`
- When a precise time is known, use it — e.g. `2024-03-17T14:30:22Z`

Use dates within the past 1–3 years unless the scenario specifies otherwise.

### Date rules — MANDATORY

**Every clinical record must include dates. A record with no date is invalid.**

Required date fields by type:

| Record type | Required date fields | Notes |
|---|---|---|
| `Encounter` | `FromTime`, `ToTime`, `EnteredOn` | Outpatient: same datetime for From/To. Inpatient: ToTime = FromTime + 1–7 days. |
| `Diagnosis` | `EnteredOn` | = encounter `FromTime`. Note: there is NO `DiagnosisTime` field. |
| `Medication` | `FromTime`, `EnteredOn` | `FromTime` = date first prescribed (may be a past encounter). `ToTime` required if discontinued. Note: there is NO `StartTime`/`StopTime`. |
| `LabOrder` | `FromTime`, `ToTime`, `EnteredOn`, `SpecimenCollectedTime`, `Result.ResultTime` | `FromTime` = `ToTime` = `EnteredOn` = encounter date. `SpecimenCollectedTime` = time specimen drawn (use encounter date + a morning time, e.g. `T08:00:00Z`). Note: there is NO `OrderedOn` field. `ResultTime` = same day (STAT) or 1–3 days later (routine). |
| `Observation` | `ObservationTime`, `EnteredOn` | Both = encounter `FromTime`. |
| `Allergy` | `EnteredOn` | `FromTime` optional (onset). |
| `Procedure` | `FromTime`, `EnteredOn` | Both = encounter datetime. `ProcedureTime` also exists but comes late in the sequence — use `FromTime` for the procedure date. |
| `Vaccination` | `FromTime`, `EnteredOn` | `FromTime` = date administered. |

**Coherence rules:**
1. `ResultTime` must be ≥ `OrderedOn` — never a result before its order.
2. Encounter `ToTime` ≥ `FromTime` — never discharge before admission.
3. Each encounter's linked records (diagnoses, labs, meds, observations) carry dates matching that encounter's `FromTime`.
4. If a patient has multiple encounters, records from encounter 1 are dated to encounter 1, not encounter 2.
5. Chronic medication `FromTime` reflects the original prescribe date, which may be months or years before the current encounter.
6. `OnsetTime` for a long-standing diagnosis (e.g., hypertension, diabetes) should plausibly predate the current encounter by years.

### Patient identity
`<PatientNumbers>` is a required wrapper around all `<PatientNumber>` entries. Each `<PatientNumber>` must include `<NumberType>` and `<Organization>` (the assigning authority). Always include at least the MRN. Add a second entry for Medicaid (MA) or Medicare (MC) if the patient has one.

```xml
<Patient>
  <PatientNumbers>
    <PatientNumber>
      <Number>MRN123456</Number>
      <NumberType>MRN</NumberType>
      <Organization>
        <Code>FACILITY1</Code>
        <Description>General Hospital</Description>
      </Organization>
    </PatientNumber>
    <PatientNumber>
      <Number>OH-MCD-4471839</Number>
      <NumberType>MA</NumberType>
      <Organization>
        <Code>OHMCD</Code>
        <Description>Ohio Medicaid</Description>
      </Organization>
    </PatientNumber>
  </PatientNumbers>
  <Name>
    <FamilyName>Smith</FamilyName>
    <GivenName>Jane</GivenName>
    <MiddleName>A.</MiddleName>
    <Type>Legal</Type>
  </Name>
  <BirthTime>1968-04-22T00:00:00Z</BirthTime>
  <Gender>
    <SDACodingStandard>HL7</SDACodingStandard>
    <Code>F</Code>
    <Description>Female</Description>
  </Gender>
  <Race>
    <SDACodingStandard>CDCREC</SDACodingStandard>
    <Code>2106-3</Code>
    <Description>White</Description>
  </Race>
  <EthnicGroup>
    <SDACodingStandard>CDCREC</SDACodingStandard>
    <Code>2186-5</Code>
    <Description>Not Hispanic or Latino</Description>
  </EthnicGroup>
  <Addresses>
    <Address>
      <Street>742 Maple Street</Street>
      <City>
        <Code>COLUMBUS</Code>
        <Description>Columbus</Description>
      </City>
      <State>
        <SDACodingStandard>USPS</SDACodingStandard>
        <Code>OH</Code>
        <Description>Ohio</Description>
      </State>
      <Zip>
        <Code>43215</Code>
      </Zip>
      <Country>
        <SDACodingStandard>ISO 3166</SDACodingStandard>
        <Code>US</Code>
        <Description>United States</Description>
      </Country>
      <County>
        <SDACodingStandard>FIPS</SDACodingStandard>
        <Code>39049</Code>
        <Description>Franklin County</Description>
      </County>
    </Address>
  </Addresses>
</Patient>
```
- MRN format: `MRN` + 6 digits
- Use realistic but fictional names, DOBs (age 18–85), and genders
- Gender, Race, EthnicGroup, and all Address sub-fields are CodeTableDetail types — always include `SDACodingStandard`

### Patient name

`<Name>` is a **complex type** — never output it as a plain string. Always use sub-elements:

| Sub-element | Required | Notes |
|---|---|---|
| `FamilyName` | Yes | Surname |
| `GivenName` | Yes | First name |
| `MiddleName` | Optional | Middle name or initial |
| `NamePrefix` | Optional | Mr., Ms., Dr. |
| `NameSuffix` | Optional | Jr., III |
| `FamilyNamePrefix` | Optional | van, de, etc. |
| `ProfessionalSuffix` | Optional | MD, PhD |
| `Type` | Recommended | Legal (default), Birth, Maiden, Display, Alias, Nickname |

Always include at least `FamilyName`, `GivenName`, and `Type`. Add `MiddleName` occasionally for realism.

```xml
<Name>
  <FamilyName>Rodriguez</FamilyName>
  <GivenName>Maria</GivenName>
  <MiddleName>L.</MiddleName>
  <Type>Legal</Type>
</Name>
```

### CodeTableDetail pattern — applies to EVERY coded field

Every element whose SDA3 type is a `CodeTableDetail` (including `Gender`, `Race`, `EthnicGroup`, `State`, `City`, `Country`, `County`, and all clinical coded fields) **must** use this structure with **`SDACodingStandard` always first**:

```xml
<FieldName>
  <SDACodingStandard>...</SDACodingStandard>
  <Code>...</Code>
  <Description>...</Description>
</FieldName>
```

The XSD sequence requires `SDACodingStandard` before `Code`. Putting `Code` or `Description` first will fail validation.

#### Coding system reference

| Field(s) | SDACodingStandard | Key codes |
|---|---|---|
| `Gender`, `BirthGender`, `LegalSex` | `HL7` | M, F, O, U |
| `Race` | `CDCREC` | 2106-3 White · 2054-5 Black · 2028-9 Asian · 1002-5 AI/AN · 2131-1 Other |
| `EthnicGroup` | `CDCREC` | 2135-2 Hispanic/Latino · 2186-5 Non-Hispanic |
| `MaritalStatus` | `HL7` | S, M, D, W, A |
| `PrimaryLanguage` | `urn:ietf:bcp:47` | en, es, fr, zh |
| `State` (in Address) | `USPS` | OH, CA, NY, TX, FL |
| `Country` (in Address) | `ISO 3166` | US, CA, GB, MX |
| `County` (in Address) | `FIPS` | 39049 (Franklin OH) · 39035 (Cuyahoga OH) |
| `City` (in Address) | *(none)* | Code + Description only |
| `Zip` (in Address) | *(none)* | Code only (5-digit ZIP) |
| Diagnoses, Problems | `ICD10` | E11.9, I10, J45.9 |
| Lab tests (`TestItemCode`) | `LN` | 4548-4 A1C · 2160-0 creatinine |
| Medications (`DrugProduct`) | `RxNorm` or `NDC` | 860975 (metformin 500mg) |
| Allergy substance | `SNM` or `RxNorm` | 372687004 amoxicillin (SNM) |
| `AllergyCategory` | `HL7` | DA (drug) · FA (food) · EA (environmental) |
| Allergy `Reaction`, `Severity` | `SNM` | 271807003 rash · 255604002 mild |
| Procedures | `CPT` or `SNM` | 99213, 93000 |
| Vaccines (`DrugProduct`/`OrderItem`) | `CVX` | 08 (hep B) · 140 (flu) · 207 (COVID mRNA) |
| Observation vital signs | **none** — omit `SDACodingStandard` entirely | 8310-5 temp · 3141-9 weight · 8480-6 systolic BP · 8462-4 diastolic BP · 8867-4 heart rate · 9279-1 resp rate · 8306-3 height |
| Route (medications) | `HL7` | PO, IV, IM, SC, INH, TOP |
| DosageForm | `NCI` | TAB, CAP, SOL, INJ |
| DoseUoM | `UCUM` | mg, mL, mcg, mg/kg |
| `DiagnosisType` | *(none)* | C (chronic) · W (working) · A (admitting) · F (final) — no SDACodingStandard |
| `AllergyStatus`, `DiagnosisStatus` | `HL7` | A (active) · R (resolved) · I (inactive) |
| `ObservationInterpretation` | `HL7` | H, L, A, N, HH, LL |

### Encounter linkage
Most clinical records should reference an encounter via `<EncounterNumber>`. Use the same value as the Encounter's `<EncounterNumber>`:
```xml
<EncounterNumber>ENC-2024031501</EncounterNumber>
```

### Encounter types
- `I` = Inpatient
- `O` = Outpatient
- `E` = Emergency
- `G` = Generated (encounter-less)

### Encounter example (Care Team + Insurance)

**CRITICAL internal order**: EncounterNumber → EncounterType → AttendingClinicians → ReferringClinician → HealthCareFacility → HealthFunds → ActionCode → EnteredBy → EnteredAt → EnteredOn → FromTime → ToTime

`HealthFunds.HealthFund.HealthFund.SDACodingStandard` = `QD_HealthPlanCodeList`. Priority=1 for primary insurance. No `SendingFacility` on Encounter.

```xml
<Encounters>
  <Encounter>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <EncounterType>O</EncounterType>
    <AttendingClinicians>
      <CareProvider>
        <Code>DR456</Code>
        <Description>Dr. Smith</Description>
      </CareProvider>
    </AttendingClinicians>
    <ReferringClinician>
      <Code>DR123</Code>
      <Description>Dr. Jones</Description>
    </ReferringClinician>
    <HealthCareFacility>
      <Organization>
        <Code>GH001</Code>
        <Description>General Hospital</Description>
      </Organization>
    </HealthCareFacility>
    <HealthFunds>
      <HealthFund>
        <HealthFund>
          <Code>BCBS</Code>
          <Description>Blue Cross Blue Shield</Description>
          <SDACodingStandard>QD_HealthPlanCodeList</SDACodingStandard>
        </HealthFund>
        <HealthFundPlan>
          <Code>BCBS-PPO</Code>
          <Description>BCBS PPO Plan</Description>
        </HealthFundPlan>
        <GroupName>Employer Group Plan</GroupName>
        <GroupNumber>GRP-12345</GroupNumber>
        <MembershipNumber>MBR-98765</MembershipNumber>
        <PlanType>PPO</PlanType>
        <Priority>1</Priority>
        <FromTime>2024-01-01T00:00:00Z</FromTime>
        <ToTime>2024-12-31T00:00:00Z</ToTime>
      </HealthFund>
    </HealthFunds>
    <ActionCode>A</ActionCode>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T08:00:00Z</FromTime>
    <ToTime>2024-03-15T14:00:00Z</ToTime>
  </Encounter>
</Encounters>
```

### ActionCode
Include `<ActionCode>A</ActionCode>` on most records. Omit it when not relevant. Other values: `U` (update), `D` (delete).

### SendingFacility / EnteredAt / EnteredBy
Use consistent fictional facility and provider codes across all records:
```xml
<SendingFacility>GH001</SendingFacility>
<EnteredAt>
  <Code>GH001</Code>
  <Description>General Hospital</Description>
</EnteredAt>
<EnteredBy>
  <Code>DR456</Code>
  <Description>Dr. Maria Reyes</Description>
</EnteredBy>
```

### Allergy example

**No `SDACodingStandard`** on nested `<Allergy>`, `<Reaction>`, or `<Severity>`. No `ActionCode`. No `AllergyCategory`. No `SendingFacility`.
`Status` is a plain string: `A`=Active, `I`=Inactive, `R`=Resolved. `ExternalId` REQUIRED before `EncounterNumber`. `EncounterNumber` is **last**.

```xml
<Allergies>
  <Allergy>
    <Allergy>
      <Code>PCN</Code>
      <Description>Penicillin</Description>
    </Allergy>
    <Reaction>
      <Code>R</Code>
      <Description>Rash</Description>
    </Reaction>
    <Severity>
      <Code>M</Code>
      <Description>Moderate</Description>
    </Severity>
    <Status>A</Status>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2023-11-05T00:00:00Z</EnteredOn>
    <FromTime>2023-11-05T00:00:00Z</FromTime>
    <ExternalId>Allergies_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
  </Allergy>
</Allergies>
```

### IllnessHistory example (Medical History tab)

Comes **after `<Allergies>`** and **before `<SocialHistories>`** in the XSD. Populates the **Medical History** tab in the Histories section of the Clinical Viewer.

`Condition.Code` and `Condition.Description` are **both set to the same plain-text description** — not a coded ICD value. No `SDACodingStandard`. No `EncounterNumber` (encounter-less). `ExternalId` REQUIRED. `FromTime` = onset date; `ToTime` = resolution date (omit for ongoing). Include chronic conditions, prior surgeries, and significant past illnesses.

```xml
<IllnessHistories>
  <IllnessHistory>
    <Condition>
      <Code>Essential hypertension</Code>
      <Description>Essential hypertension</Description>
    </Condition>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2018-04-10T00:00:00Z</FromTime>
    <ExternalId>IllnessHistories_1</ExternalId>
  </IllnessHistory>
  <IllnessHistory>
    <Condition>
      <Code>Appendectomy (remote surgical history)</Code>
      <Description>Appendectomy (remote surgical history)</Description>
    </Condition>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>1995-06-01T00:00:00Z</FromTime>
    <ToTime>1995-06-08T00:00:00Z</ToTime>
    <ExternalId>IllnessHistories_2</ExternalId>
  </IllnessHistory>
</IllnessHistories>
```

### SocialHistory example

Comes **after `<IllnessHistories>`** and **before `<Diagnoses>`** in the XSD. **No `SDACodingStandard`** on `SocialHabit`. No `EncounterNumber` (encounter-less). `ExternalId` REQUIRED.

**CRITICAL: There is NO `<Clinician>` field in SocialHistory — never add one.** The XSD does not define it.

XSD field order: `SocialHabit → SocialHabitComments → Status → EnteredBy → EnteredAt → EnteredOn → FromTime → ExternalId`

Common `SocialHabit` codes: `NS`=Non Smoker · `TOBA`=Tobacco/Smoker · `ETOH`=Alcohol · `NOALC`=No Alcohol · `DRUGS`=Drug use · `EX-SMOK`=Former smoker.

```xml
<SocialHistories>
  <SocialHistory>
    <SocialHabit>
      <Code>NS</Code>
      <Description>Non Smoker</Description>
    </SocialHabit>
    <SocialHabitComments>Patient has never smoked.</SocialHabitComments>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <ExternalId>SocialHistories_1</ExternalId>
  </SocialHistory>
</SocialHistories>
```

### FamilyHistory example

Comes **after `<SocialHistories>`** and **before `<Diagnoses>`**. No `SDACodingStandard` on `FamilyMember` or `Diagnosis`. `ExternalId` REQUIRED.

Common `FamilyMember` codes: `FTH`=Father · `MTH`=Mother · `SIB`=Sibling · `CHILD`=Child · `GRNDF`=Grandfather · `GRNDF`=Grandmother.

```xml
<FamilyHistories>
  <FamilyHistory>
    <FamilyMember>
      <Code>FTH</Code>
      <Description>Father</Description>
    </FamilyMember>
    <Diagnosis>
      <Code>I10</Code>
      <Description>Essential hypertension</Description>
    </Diagnosis>
    <NoteText>Father had hypertension diagnosed in his 50s.</NoteText>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>FamilyHistories_1</ExternalId>
  </FamilyHistory>
</FamilyHistories>
```

### SocialDeterminants example

Comes **after `<Vaccinations>`** near the end of `<Container>`. `Status` is plain string `"Active"`. `SocialDeterminantCode` uses **`Description` only** — no `Code`, no `SDACodingStandard`. `SocialDeterminantValueText` holds the answer. No `EncounterNumber`. `ExternalId` REQUIRED.

```xml
<SocialDeterminants>
  <SocialDeterminant>
    <Status>Active</Status>
    <SocialDeterminantCode>
      <Description>Housing status</Description>
    </SocialDeterminantCode>
    <SocialDeterminantValueText>Owner</SocialDeterminantValueText>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>SocialDeterminants_1</ExternalId>
  </SocialDeterminant>
  <SocialDeterminant>
    <Status>Active</Status>
    <SocialDeterminantCode>
      <Description>History of employment status NIOSH</Description>
    </SocialDeterminantCode>
    <SocialDeterminantValueText>Employed full-time</SocialDeterminantValueText>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>SocialDeterminants_2</ExternalId>
  </SocialDeterminant>
</SocialDeterminants>
```

### Observation / vital signs example

**XSD field order**: `ExternalId → EncounterNumber → ObservationTime → ObservationCode → ObservationValue → EnteredBy → EnteredAt → EnteredOn`

**CRITICAL**: `ExternalId` is the **very first** element — never place it after `ObservationCode`, `ObservationValue`, `EnteredAt`, or `EnteredOn`. `ObservationCode` must **NOT** include `SDACodingStandard`. No `ActionCode` on Observations.

Confirmed vital-sign codes: `8480-6` BP Systolic · `8462-4` BP Diastolic · `8867-4` Heart Rate · `8310-5` Temperature · `9279-1` Respiratory Rate · `3141-9` Weight · `8306-3` Height

```xml
<Observations>
  <Observation>
    <ExternalId>Observations_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <ObservationTime>2024-03-15T10:00:00Z</ObservationTime>
    <ObservationCode>
      <Code>8480-6</Code>
      <Description>BP Systolic</Description>
    </ObservationCode>
    <ObservationValue>142</ObservationValue>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T10:00:00Z</EnteredOn>
  </Observation>
</Observations>
```

### Medication example

Note: `Medication.Status` is a **plain string** (`active`, `C`=completed, `D`=discontinued) — not a CodeTableDetail.
Use `FromTime` for prescription start date. Use `DoseQuantity`+`DoseUoM` (not `<Dose>`).
**No `ActionCode`.** **No `SDACodingStandard`** on `DrugProduct`, `DoseUoM`, or `Route`.
`ExternalId` REQUIRED. `EncounterNumber` comes early (after `FromTime`/`ExternalId`).

```xml
<Medications>
  <Medication>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-01-01T00:00:00Z</FromTime>
    <ExternalId>Medications_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <Frequency><Code>BID</Code><Description>Twice daily</Description></Frequency>
    <Status>active</Status>
    <DrugProduct>
      <Code>860975</Code>
      <Description>Metformin 500 MG Oral Tablet</Description>
    </DrugProduct>
    <DoseQuantity>500</DoseQuantity>
    <DoseUoM><Code>mg</Code><Description>mg</Description></DoseUoM>
    <DosageForm><Code>TAB</Code><Description>Tablet</Description></DosageForm>
    <Route><Code>PO</Code><Description>Oral</Description></Route>
  </Medication>
</Medications>
```

### Lab order / result example

Note: Use `FromTime` for the order date — there is NO `OrderedOn` field. `ToTime` = `FromTime` for most orders. `EncounterNumber` is LAST.
`ResultStatus` is a **plain string** (`F`=Final, `R`=Entered, `K`=Corrected) — not a CodeTableDetail.

Critical ordering rules:
- `OrderItem`, `OrderedBy`, `EnteringOrganization`, `Specimen` come **before** `ActionCode`/`EnteredBy`/`EnteredOn`/`FromTime`/`ToTime`
- `SpecimenCollectedTime` and `Priority` come **after** `Specimen`, **before** `ActionCode`
- `ToTime` REQUIRED: same value as `FromTime` for most orders
- `ResultType` must be **first** inside `<Result>`: use `AT` for atomic results (ResultItems), `IM` for text
- Inside `<LabResultItem>`: `EnteredOn` comes **before** `TestItemCode` (XSD sequence positions 2 vs 8)
- `ResultNormalRange` is a plain string — never use `ReferenceRange` as plain text
- `ExternalId` REQUIRED at three levels: on `LabOrder` (before `EncounterNumber`), on `Result`, on each `LabResultItem`

```xml
<LabOrders>
  <LabOrder>
    <PlacerId>LAB-20240315-001</PlacerId>
    <OrderItem>
      <SDACodingStandard>LN</SDACodingStandard>
      <Code>2339-0</Code>
      <Description>Glucose panel</Description>
    </OrderItem>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <EnteringOrganization><Code>GH001</Code><Description>General Hospital</Description></EnteringOrganization>
    <Specimen>Blood</Specimen>
    <SpecimenCollectedTime>2024-03-15T08:00:00Z</SpecimenCollectedTime>
    <Priority><Code>R</Code><Description>Routine</Description></Priority>
    <ActionCode>A</ActionCode>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <ToTime>2024-03-15T00:00:00Z</ToTime>
    <Result>
      <ResultType>AT</ResultType>
      <ResultItems>
        <LabResultItem>
          <EnteredOn>2024-03-17T14:30:22Z</EnteredOn>
          <TestItemCode>
            <SDACodingStandard>LN</SDACodingStandard>
            <Code>2339-0</Code>
            <Description>Glucose [Mass/volume] in Blood</Description>
            <IsNumeric>true</IsNumeric>
          </TestItemCode>
          <ResultValue>142</ResultValue>
          <ResultValueUnits>mg/dL</ResultValueUnits>
          <ResultNormalRange>70-99</ResultNormalRange>
          <ResultInterpretation>H</ResultInterpretation>
          <ExternalId>LabResultItem_1</ExternalId>
        </LabResultItem>
      </ResultItems>
      <ResultTime>2024-03-17T14:30:22Z</ResultTime>
      <ResultStatus>F</ResultStatus>
      <ExternalId>Result_1</ExternalId>
    </Result>
    <ExternalId>LabOrder_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
  </LabOrder>
</LabOrders>
```

### Diagnosis example

**Critical**: The diagnosis code lives in a **child `<Diagnosis>` element** (same name as parent). No `SDACodingStandard` on the nested code or `DiagnosisType`. No `ActionCode`. `EncounterNumber` is **first**. `ExternalId` is **last**.

```xml
<Diagnoses>
  <Diagnosis>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <DiagnosingClinician><Code>DR456</Code><Description>Dr. Smith</Description></DiagnosingClinician>
    <Diagnosis>
      <Code>I10</Code>
      <Description>Essential hypertension</Description>
    </Diagnosis>
    <DiagnosisType>
      <Code>C</Code>
      <Description>Chronic</Description>
    </DiagnosisType>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ExternalId>Diagnoses_1</ExternalId>
  </Diagnosis>
</Diagnoses>
```

### Problem example

**Critical**: The problem code lives in a **child `<Problem>` element** (same name as the parent record, type `BodyPart`). It comes **before** ActionCode, EnteredBy, EnteredOn, and FromTime in the sequence.

```xml
<Problems>
  <Problem>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <Problem>
      <SDACodingStandard>ICD10</SDACodingStandard>
      <Code>I50.9</Code>
      <Description>Heart failure, unspecified</Description>
    </Problem>
    <Status>
      <SDACodingStandard>SNOMED CT</SDACodingStandard>
      <Code>55561003</Code>
      <Description>Active</Description>
    </Status>
    <ActionCode>A</ActionCode>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
  </Problem>
</Problems>
```

### Document example (clinical notes)

**No `SDACodingStandard`** on `DocumentType`. `ExternalId` REQUIRED. `NoteText` is plain text (not Base64). Include `<Documents>` for every encounter — at minimum a progress note for outpatient visits; H&P + Discharge Summary for inpatient visits.

DocumentType codes: `PN`=Progress Note, `DS`=Discharge Summary, `HP`=History & Physical, `SV`=Sick Visit.

```xml
<Documents>
  <Document>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <DocumentTime>2024-03-15T14:00:00Z</DocumentTime>
    <NoteText>CHIEF COMPLAINT: Follow-up for hypertension.

HISTORY OF PRESENT ILLNESS: Patient presents for routine follow-up. Reports good medication compliance. Denies chest pain or shortness of breath.

PHYSICAL EXAMINATION:
Vitals: BP 138/82 mmHg, HR 74 bpm, Weight 185 lbs
Cardiovascular: Regular rate and rhythm, no murmurs.

ASSESSMENT AND PLAN:
1. Essential hypertension - BP slightly elevated. Continue lisinopril, reinforce low-sodium diet.

FOLLOW-UP: Return in 3 months.</NoteText>
    <DocumentType>
      <Code>PN</Code>
      <Description>Progress Note</Description>
    </DocumentType>
    <Clinician><Code>DR456</Code><Description>Dr. Smith</Description></Clinician>
    <DocumentName>Outpatient Progress Note</DocumentName>
    <FileType>Progress Note</FileType>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T14:00:00Z</EnteredOn>
    <ExternalId>Documents_1</ExternalId>
  </Document>
</Documents>
```

For **inpatient encounters** include two documents: an H&P (`Code="HP"`) on admission and a Discharge Summary (`Code="DS"`) on discharge. The DS text must include hospital course, discharge diagnoses, discharge medications, and follow-up instructions.

### Vaccination example

**No `ActionCode`**. **No `SDACodingStandard`** on `OrderItem`, `DrugProduct`, `DoseUoM`, `DosageForm`, or `Route`. `OrderItem` and `DrugProduct` use the same CVX code. `Status` is the plain string `V` (given/verified).

**Vaccination extends `SuperClass`** — the `SuperClass` fields come **FIRST** in the effective XSD sequence, before all clinical/drug fields.

**CRITICAL XSD field order**: `EnteredBy → EnteredAt → EnteredOn → FromTime → ExternalId → EncounterNumber → OrderItem → OrderedBy → Frequency → Status → DrugProduct → DoseQuantity → DoseUoM → DosageForm → Route → Indication`

```xml
<Vaccinations>
  <Vaccination>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2022-10-01T00:00:00Z</EnteredOn>
    <FromTime>2022-10-01T00:00:00Z</FromTime>
    <ExternalId>Vaccinations_1</ExternalId>
    <EncounterNumber>ENC-2022100101</EncounterNumber>
    <OrderItem><Code>141</Code><Description>Influenza, seasonal, injectable</Description></OrderItem>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <Frequency><Code>O</Code><Description>Once</Description></Frequency>
    <Status>V</Status>
    <DrugProduct>
      <Code>141</Code>
      <Description>Influenza, seasonal, injectable</Description>
    </DrugProduct>
    <DoseQuantity>1</DoseQuantity>
    <DoseUoM><Code>dose</Code><Description>dose</Description></DoseUoM>
    <DosageForm><Code>I</Code><Description>injection</Description></DosageForm>
    <Route><Code>IM</Code><Description>intramuscular</Description></Route>
    <Indication>vaccination</Indication>
  </Vaccination>
</Vaccinations>
```

### Procedure example

**Critical**: The procedure code lives in a **child `<Procedure>` element** (same name as the parent record). Never put `<SDACodingStandard>` directly inside the outer `<Procedure>` — it must be wrapped in the inner `<Procedure>`.

```xml
<Procedures>
  <Procedure>
    <FromTime>2024-03-15T00:00:00Z</FromTime>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
    <Procedure>
      <SDACodingStandard>CPT</SDACodingStandard>
      <Code>99213</Code>
      <Description>Office visit, moderate complexity</Description>
    </Procedure>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <ActionCode>A</ActionCode>
    <SendingFacility>GH001</SendingFacility>
  </Procedure>
</Procedures>
```

### RadOrder example (Diagnostic Studies tab)

`<RadOrders>` comes **after `<LabOrders>`** and **before `<Medications>`**. Result is a narrative `<ResultText>` (not ResultItems). No `SDACodingStandard` on `OrderItem`. `ExternalId` REQUIRED. `EncounterNumber` is **last**.

**CRITICAL XSD field order**: `PlacerId → OrderItem → OrderedBy → Specimen → SpecimenCollectedTime → EnteredBy → EnteredAt → EnteredOn → FromTime → ToTime → Result → ReasonForStudy → ExternalId → EncounterNumber`

- `OrderedBy` MUST come **before** `Specimen`
- `Result` and `ReasonForStudy` come **after** `FromTime`/`ToTime`
- `EncounterNumber` is the **last** element

```xml
<RadOrders>
  <RadOrder>
    <PlacerId>RAD-20240315-001</PlacerId>
    <OrderItem>
      <Code>CXR-PA-LAT</Code>
      <Description>Chest X-Ray PA and Lateral</Description>
    </OrderItem>
    <OrderedBy><Code>DR456</Code><Description>Dr. Smith</Description></OrderedBy>
    <Specimen>Chest radiograph</Specimen>
    <SpecimenCollectedTime>2024-03-15T08:30:00Z</SpecimenCollectedTime>
    <EnteredBy><Code>DR456</Code><Description>Dr. Smith</Description></EnteredBy>
    <EnteredAt><Code>GH001</Code><Description>General Hospital</Description></EnteredAt>
    <EnteredOn>2024-03-15T00:00:00Z</EnteredOn>
    <FromTime>2024-03-15T08:30:00Z</FromTime>
    <ToTime>2024-03-15T08:30:00Z</ToTime>
    <Result>
      <ResultTime>2024-03-15T11:00:00Z</ResultTime>
      <ResultText>No acute cardiopulmonary process. Borderline cardiac enlargement. Lungs clear bilaterally.</ResultText>
      <EnteredOn>2024-03-15T08:30:00Z</EnteredOn>
      <AuthorizationTime>2024-03-15T11:00:00Z</AuthorizationTime>
    </Result>
    <ReasonForStudy>
      <Description>Evaluate for cardiomegaly</Description>
    </ReasonForStudy>
    <ExternalId>RadOrders_1</ExternalId>
    <EncounterNumber>ENC-2024031501</EncounterNumber>
  </RadOrder>
</RadOrders>
```

Common `OrderItem` codes for Diagnostic Studies: `CXR-PA-LAT` Chest X-Ray · `EKG-12L` 12-Lead ECG · `CT-HEAD` CT Head · `MRI-BRAIN` MRI Brain · `US-ABD` Abdominal Ultrasound · `ECHO-TTE` Transthoracic Echo · `DEXA` Bone Density · `MAMMO` Mammogram

### MedicalClaim example (Claims section)

Comes **after `<Vaccinations>`** and **before `<SocialDeterminants>`**. `AdjudicatedCoverage` is a **plain string** (insurer name). `ProcedureCode` in `MedicalClaimLine` uses `Description` only (free text). `MedicalClaimNumber` = unique claim identifier.

```xml
<MedicalClaims>
  <MedicalClaim>
    <MedicalClaimNumber>CLM-20240315-001</MedicalClaimNumber>
    <UpdatedOn>2024-03-20T00:00:00Z</UpdatedOn>
    <FromTime>2024-03-15T08:00:00Z</FromTime>
    <ToTime>2024-03-15T14:00:00Z</ToTime>
    <ClaimProcessedDate>2024-03-20T00:00:00Z</ClaimProcessedDate>
    <ReceivedDate>2024-03-16T00:00:00Z</ReceivedDate>
    <SubmissionDate>2024-03-16T00:00:00Z</SubmissionDate>
    <AdjudicatedCoverage>Blue Cross Blue Shield</AdjudicatedCoverage>
    <AdjudicationStatus><Code>complete</Code><Description>complete</Description></AdjudicationStatus>
    <Priority><Code>N</Code><Description>normal</Description></Priority>
    <Status><Code>active</Code><Description>active</Description></Status>
    <BillingProvider><Code>GH001</Code><Description>General Hospital</Description></BillingProvider>
    <ClaimType><Code>Professional</Code><Description>Professional</Description></ClaimType>
    <Payer><Code>BCBS</Code><Description>Blue Cross Blue Shield</Description></Payer>
    <PrimaryProcedure>
      <Procedure><Description>Office or other outpatient visit, moderate complexity (99213)</Description></Procedure>
    </PrimaryProcedure>
    <PaymentStatus><Code>paid</Code><Description>paid</Description></PaymentStatus>
    <PaymentAmount>120</PaymentAmount>
    <MedicalClaimLines>
      <MedicalClaimLine>
        <LineCounter>1</LineCounter>
        <FromTime>2024-03-15T08:00:00Z</FromTime>
        <ToTime>2024-03-15T14:00:00Z</ToTime>
        <ProcedureCode><Description>Office or other outpatient visit, moderate complexity (99213)</Description></ProcedureCode>
        <AdjudicationDetails>
          <AdjudicationDetail>
            <Category><Code>submitted</Code><Description>submitted</Description></Category>
          </AdjudicationDetail>
        </AdjudicationDetails>
      </MedicalClaimLine>
    </MedicalClaimLines>
  </MedicalClaim>
</MedicalClaims>
```

### What NOT to include unless specifically relevant
- `CustomPairs`, `ProvenanceIds`, `SdaID`, `SourceFormat` — system/internal fields, omit
- `Extension` elements — omit unless testing extensions
- `ObservationGroups`, `Referrals`, `PharmacyClaims`, `GenomicsOrders` — omit unless scenario calls for them
- `MPIID` — omit unless testing registry scenarios
- `MothersMaidenName`, `BlankNameReason` — omit unless scenario requires

### Output
Wrap the XML in a markdown XML code fence for readability. No explanation before or after — only the fenced XML block.
Start the block with ` ```xml ` and end with ` ``` `. The XML itself starts with `<Container>` and ends with `</Container>`.
If the scenario is ambiguous, pick a reasonable clinical interpretation and generate the data.
