# data_sender.py
# API functions

import json
import requests2 as requests

DEBUG = True  # flip to False once everything works
IPDATA_KEY = "e2f1b4d9820c7256c8ccf858c57a98d71319a84664c01d23886f1ef6"


def _log(*args):
    if DEBUG:
        print("[api]", *args)
        
#################################
def get_location_from_ip(api_key: str):
    """
    Fetches the location based on IP using the ipdata.co API.
    Returns the city if found, otherwise returns the region.
    """
    url = f"https://api.ipdata.co/?api-key={api_key}"
    headers = {"accept": "application/json"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code >= 400:
            _log("HTTP error:", response.status_code)
            return "Lausanne"

        data = response.json()

        # Return city if it exists and is not empty, otherwise return region
        return data.get("city") or data.get("region")
        
    except Exception as e:
        _log("Error fetching location data:", e)
        return "Lausanne" # Default if no connection

