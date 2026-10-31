import urllib.error
import urllib.request

# 1. Paste your active token and copied WABA ID here:
token = "EAGjFKoSHYUgBSz6D9I9pBxgUgvgUWUkdbYbhZBQeBZBEV4h9yTKAOKTtWpu9BaIKwaAXhW1IwiZAEFKiKYeAWWLVqKdnLv8zpCthjHKLx9hGcDq7nWHMAEEahDympkJXoPUs7YEZCsiX1uzZBkBZA1dt9meT8EbCn3bYETj9aBbEMZAAlt1kxlWi7gdjz6Mx8GLpU4471T1PvJ9DuZBFQZByZCofHbn1aGQshrXkpkRoyfBza6Y1jbg5nwjrBOWDy2XjI2PAUBtcShUiObF45JKeaq"
waba_id = "29604353555856022"

# 2. Subscribe your app to the WABA
url = f"https://graph.facebook.com/v21.0/{waba_id}/subscribed_apps"
req = urllib.request.Request(
    url,
    data=b"",
    headers={"Authorization": f"Bearer {token}"},
    method="POST",
)

try:
  with urllib.request.urlopen(req) as resp:
    print("RESULT:", resp.read().decode())
except urllib.error.HTTPError as e:
  print(f"HTTP {e.code} Error: {e.read().decode()}")
except Exception as e:
  print("Error:", e)