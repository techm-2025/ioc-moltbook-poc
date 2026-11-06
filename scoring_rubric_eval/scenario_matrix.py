"""
scenario_matrix.py
Generates 50 distinct combinations of (T1, T2, T3) scenarios.
"""

T1_DILEMMAS = [
    ("NYU (no financial aid, debt ceiling breach risk) vs. ASU ($25,000/yr scholarship, out-of-pocket <$20,000/yr)"),
    ("Columbia University ($85,000/yr full sticker price) vs. Georgia Tech (In-state tuition, $15,000/yr net)"),
    ("USC (private debt required, $70,000/yr) vs. UIUC (Grainger Engineering, $30,000/yr covered by savings)"),
    ("Carnegie Mellon University (no grant, $82,000/yr) vs. Purdue University (Honors college, $22,000/yr out-of-pocket)"),
    ("Boston University ($65,000/yr parent PLUS loans) vs. UMass Amherst (Commonwealth Honors, $18,000/yr net)"),
    ("Northwestern University (breaches family debt ceiling) vs. University of Wisconsin-Madison (merit award $15,000/yr)"),
    ("Johns Hopkins University (pre-med track, full debt) vs. University of Maryland ($30,000/yr Banneker/Key scholarship)"),
    ("Duke University ($80,000/yr cost of attendance) vs. UNC Chapel Hill ($24,000/yr covenant scholar program)"),
    ("Georgetown University (unfunded transfer) vs. Penn State University (Schreyer Honors, funded reserve)"),
    ("Vanderbilt University (unsubsidized loans) vs. University of Florida ($10,000/yr net cost of attendance)")
]

T2_PROFILES = [
    ("MS CS Admission: 3.6 undergraduate GPA, 2 software internships, 91st percentile GRE Quant, 1 workshop paper"),
    ("MS Data Science Admission: 3.3 GPA in Math, 4 Kaggle Grandmaster ranks, 3 years industry SWE, no GRE"),
    ("MS Robotics Admission: 3.7 GPA in MechE, 1 patent filed, 88th percentile GRE Quant, missing OS prerequisite"),
    ("MS Artificial Intelligence: 3.5 GPA in CS, 1 NeurIPS student paper, low verbal GRE, strong GitHub repo"),
    ("MS Cybersecurity: 3.4 GPA, DoD active clearance, 3 years network engineer experience, no research publications")
]

T3_MECHANISMS = [
    ("Executive education loan protection rider: 60% coverage ratio, 6-month involuntary job-loss waiting period"),
    ("FinTech Exec Credential ISA (Income Share Agreement): 10% salary clawback clause with 1.8x payment cap"),
    ("Executive Certificate bridge loan: debt-moratorium rider excluding voluntary company restructuring"),
    ("MicroMasters financing structure: interest-capitalization deferral with restrictive disability waiver"),
    ("Part-time Executive MS loan: employer repayment offset rider with 24-month retention clawback clause")
]

def get_scenarios(total_count: int = 50):
    scenarios = []
    index = 0
    while len(scenarios) < total_count:
        t1 = T1_DILEMMAS[index % len(T1_DILEMMAS)]
        t2 = T2_PROFILES[(index // len(T1_DILEMMAS)) % len(T2_PROFILES)]
        t3 = T3_MECHANISMS[index % len(T3_MECHANISMS)]
        scenarios.append({
            "run_id": index + 1,
            "post_id": f"post-{1000 + index + 1}",
            "t1": t1,
            "t2": t2,
            "t3": t3
        })
        index += 1
    return scenarios