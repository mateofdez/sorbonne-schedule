import os
import requests

os.makedirs("debug", exist_ok=True)

for grupo in ["M1.INFO.STL", "M1.MATH"]:
    for sem in [4, 5]:
        url = f"https://planning.upmc.fr/oldEmpSelect/jussieu/{grupo}/{sem}/"
        r = requests.get(url, timeout=30)
        print(url, r.status_code, len(r.content))
        with open(f"debug/{grupo}_{sem}.html", "wb") as f:
            f.write(r.content)
