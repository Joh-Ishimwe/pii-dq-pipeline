"""
Generates a deliberately messy `customers_raw.csv` for the DQ project.

This file exists ONLY because we need synthetic data that is safe to commit.
Never generate this from, or replace it with, real production data.

Uses Faker for the underlying "clean" values (street addresses, raw phone
digits, dates of birth, account-creation dates) so the base data is realistic
and varied. The MESSINESS - missing values, inconsistent formats, invalid
values, duplicates - is still injected deterministically (tied to the row
index), because the data quality report and every downstream report quote
exact defect counts.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

SEED = 11
random.seed(SEED)
fake = Faker()
fake.seed_instance(SEED)

# Fixed, absolute date ranges - NOT "today" or "-Ny". Faker's relative anchors
# (`end_date="today"`) would make this file's contents depend on the real
# calendar date it happens to be regenerated on, breaking the one guarantee
# this module makes: same seed -> same messy file, forever.
DOB_START, DOB_END = date(1955, 1, 1), date(2015, 12, 31)     # spans ages
                                                                # ~11-71 as of
                                                                # "today" in this
                                                                # dataset's fiction
                                                                # - deliberately
                                                                # includes minors
CREATED_START = date(2019, 1, 1)
CREATED_END = CREATED_START + timedelta(days=2400)

OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "customers_raw.csv"

FIRST = ["john", "MARY", "Kwame", "amina", "Grace", "peter", "Chidi", "Fatou",
         "David", "Nia", "samuel", "Aisha", "Emeka", "Zainab", "Thomas",
         "leila", "Kofi", "Rita", "michael", "Yaa"]
LAST = ["doe", "SMITH", "Mensah", "okafor", "Diallo", "brown", "Adeyemi",
        "Nkrumah", "williams", "Osei", "TRAORE", "Johnson", "Balogun",
        "keita", "Owusu", "Garcia", "abubakar", "Lee", "Sarr", "Mwangi"]
CITIES = ["Kigali", "Accra", "Lagos", "Nairobi", "Dakar", "Kampala"]

STATUS_POOL = (["active"] * 40 + ["inactive"] * 15 + ["suspended"] * 8
               + ["Active", "ACTIVE", "Inactive", "pending", "ACTV", ""])

EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "company.co", "mail.rw"]


def messy_phone(i: int) -> str:
    """A Faker-generated number, in seven different human formats. This is
    a consistency defect - the underlying number is fine, the formatting isn't."""
    # NANP-shaped: area code can't start 0/1. Faker gives us the raw digits;
    # we just enforce that one real-world constraint.
    digits = fake.numerify("#########")
    a = str(random.randint(2, 9)) + digits[:2]
    b, c = digits[2:5], digits[5:9]
    style = i % 8
    return [
        f"{a}-{b}-{c}",
        f"({a}) {b}-{c}",
        f"{a}.{b}.{c}",
        f"+1{a}{b}{c}",
        f"{a}{b}{c}",
        f"  {a}-{b}-{c}  ",
        f"{a}-{b}-{c} ext.12",
        "555-CALL-NOW",
    ][style]


def messy_date(d, i: int) -> str:
    """A Faker-generated date, in five different formats, plus junk. A
    validity + consistency defect."""
    style = i % 7
    if style == 5:
        return "invalid_date"
    if style == 6:
        return ""
    return [
        d.isoformat(),                 # 1990-05-14
        d.strftime("%d/%m/%Y"),        # 14/05/1990
        d.strftime("%m/%d/%Y"),        # 05/14/1990  <-- ambiguous vs above!
        d.strftime("%d-%b-%Y"),        # 14-May-1990
        d.strftime("%Y/%m/%d"),        # 1990/05/14
    ][style]


def messy_email(f: str, l: str, i: int) -> str:
    base = f"{f.lower()}.{l.lower()}"
    style = i % 12
    if style == 9:
        return f"{base}@@{random.choice(EMAIL_DOMAINS)}"   # invalid
    if style == 10:
        return f"{base}.at.{random.choice(EMAIL_DOMAINS)}"  # missing @
    if style == 11:
        return ""                                           # missing
    if style == 7:
        return f"  {base}@{random.choice(EMAIL_DOMAINS).upper()}  "  # whitespace + case
    return f"{base}@{random.choice(EMAIL_DOMAINS)}"


def messy_address(i: int) -> str:
    """A Faker street address, paired with a curated city. Faker's own
    'address' provider mixes in state/zip formats that don't fit a customer
    record here, so we take just the street portion and build the rest ourselves."""
    return f"{fake.street_address()}, {random.choice(CITIES)}"


rows = []
used_ids = []

for i in range(220):
    cid = 1000 + i
    # 6 duplicated ids injected
    if i in (37, 88, 141, 190):
        cid = used_ids[i - 30]
    used_ids.append(cid)

    f = random.choice(FIRST)
    l = random.choice(LAST)

    dob = fake.date_between_dates(date_start=DOB_START, date_end=DOB_END)
    created = fake.date_between_dates(date_start=CREATED_START, date_end=CREATED_END)

    # income defects
    r = random.random()
    if r < 0.05:
        income = ""
    elif r < 0.08:
        income = str(-random.randint(1000, 90000))          # negative
    elif r < 0.10:
        income = str(random.randint(11_000_000, 99_000_000))  # above cap
    elif r < 0.14:
        income = f"${random.randint(20000, 180000):,}"       # currency string
    else:
        income = str(round(random.uniform(9000, 240000), 2))

    # name defects
    fn, ln = f, l
    if i % 23 == 0:
        fn = ""
    if i % 31 == 0:
        ln = "X"                       # too short
    if i % 29 == 0:
        fn = f"{f}123"                 # non-alphabetic

    # address defects
    if i % 17 == 0:
        addr = ""
    elif i % 19 == 0:
        addr = "N/A"                   # short + a null-in-disguise
    else:
        addr = messy_address(i)

    # a few impossible ages
    dob_out = messy_date(dob, i)
    if i % 47 == 0:
        dob_out = "1850-03-15"         # age > 150

    rows.append({
        "customer_id": str(cid),
        "first_name": fn,
        "last_name": ln,
        "email": messy_email(f, l, i),
        "phone": messy_phone(i),
        "date_of_birth": dob_out,
        "address": addr,
        "income": income,
        "account_status": random.choice(STATUS_POOL),
        "created_date": messy_date(created, i + 2),
    })

# 3 exact full-row duplicates (a different defect from duplicate IDs)
rows.extend([dict(rows[12]), dict(rows[64]), dict(rows[155])])
random.shuffle(rows)

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print(f"wrote {len(rows)} rows -> {OUT}")
