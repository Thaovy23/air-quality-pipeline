"""fetch.py - Fetch air quality data from IQAir API and write to Postgres."""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

IQAIR_API_KEY = os.getenv("IQAIR_API_KEY")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")


def fetch_data():
      """Fetch nearest station data from IQAir API."""
      # TODO: implement fetch + normalize + write to Postgres
      pass


if __name__ == "__main__":
      fetch_data()
  
