import json, os, sys, urllib.request, urllib.error

QUERY = " ".join(sys.argv[1:]) or "one limited ICU bed"
URL   = os.environ.get("ALLOCATION_URL", "http://localhost:8081/auction")
KEY   = os.environ.get("ALLOCATION_API_KEY", "")

req = urllib.request.Request(
    URL,
    data=json.dumps({"query": QUERY}).encode(),
    headers={"content-type": "application/json", **({"X-API-Key": KEY} if KEY else {})},
)
try:
    body = urllib.request.urlopen(req, timeout=30).read()
except urllib.error.HTTPError as exc:
    print(f"{exc.code}: {json.loads(exc.read() or b'{}').get('error', exc.reason)}")
    sys.exit(1)

j   = json.loads(body)
res = round(j["reserve_price"], 1)
clr = "cleared" if j["winning_bid"] and j["winning_bid"] >= j["reserve_price"] else "BELOW RESERVE"
u   = " | ".join(f"{k.split('-')[0].lower()} {round(v, 1)}"
                 for k, v in sorted(j["utilities"].items(), key=lambda kv: -kv[1]))

print(f"query     : {j['query']}")
print(f"resource  : {j['resource']['type']}")
print(f"outcome   : {j['outcome']}")
print(f"winner    : {j['winner']} ({j['winning_candidate_id']})  bid {j['winning_bid']}   reserve {res}   {clr}")
print(f"utilities : {u}")
