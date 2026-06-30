import pandas as pd, sys
sys.path.insert(0, "/tmp/FFIS")
import ffis_email_pipeline as P

def df(d): return pd.DataFrame(d)

# 1) Classification across object types (heuristic, no API key)
contacts = df({"FirstName":["A"],"LastName":["B"],"Email":["a@b.com"],"AccountId":["001x"],"Title":["Mgr"]})
leads = df({"FirstName":["A"],"LastName":["B"],"Company":["X"],"Status":["New"],"LeadSource":["Web"]})
opps = df({"Name":["Deal"],"StageName":["Won"],"CloseDate":["2025-01-01"],"Amount":["100"],"AccountId":["001x"]})
assert P.classify_object_type(contacts).object_type == "Contact"
assert P.classify_object_type(leads).object_type == "Lead"
assert P.classify_object_type(opps).object_type == "Opportunity"
print("classify: Contact/Lead/Opportunity OK")

# 2) Validation: bad email + unparseable date + blank required
opp = df({
    "Name":["Deal1","Deal2","Deal3",""],
    "StageName":["Won","Won","Won","Won"],
    "CloseDate":["2025-01-01","not-a-date","2025-03-01","2025-04-01"],
    "Amount":["100","200","xyz","400"],
    "AccountId":["001a","001b","001c","001d"],
})
good_mask, reasons = P.validate_records(opp, "Opportunity")
assert good_mask.tolist() == [True, False, False, False], good_mask.tolist()
assert any("CloseDate" in r for r in reasons[1])
assert any("Amount" in r for r in reasons[2])
assert any("Name" in r for r in reasons[3])
print("validate: date/numeric/required reasons OK")

# 3) Routing precedence: duplicate removed before validation; counts sum
data = df({
    "Name":["Deal1","Deal1","Deal2",""],   # Deal1 repeated -> within-file dup
    "StageName":["Won"]*4,
    "CloseDate":["2025-01-01"]*4,
    "Amount":["100","100","200","300"],
    "AccountId":["a","a","b","d"],
})
r = P.route_records(data, "opps.csv", dedup=P.NullDedup())
c = r.counts
assert c["total"] == 4
assert c["good"] == 2          # Deal1(first), Deal2
assert c["duplicate"] == 1     # Deal1(second)
assert c["bad"] == 1           # blank Name
print("route: counts", c, "OK")

# 4) Email format validation on Contact
con = df({"LastName":["X","Y"],"AccountId":["1","2"],"Email":["good@x.com","bad-email"]})
gm, rs = P.validate_records(con, "Contact")
assert gm.tolist() == [True, False]
print("validate: contact email OK")

# 5) Snowflake dedup factory selects correct backend without creds
assert P.make_dedup_source("none").name == "none"
assert P.make_dedup_source("csv","/tmp/refs").name == "csv"
assert P.make_dedup_source("snowflake").name == "snowflake"
print("dedup factory OK")

print("\nALL TESTS PASSED")
